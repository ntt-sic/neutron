# Copyright 2013 OpenStack Foundation.
# Copyright(c)2013 NTT corp.
# Copyright 2013 New Dream Network, LLC (DreamHost)
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Mark McClain, DreamHost
# @author: IWAMOTO Toshihir, VA Linux Systems Japan

import uuid

from neutron import context as neutron_context
from neutron.common import constants as q_const
from neutron.common import exceptions as q_exc
from neutron.common import rpc as q_rpc
from neutron.common import topics
from neutron.db import agents_db
from neutron.db import api as qdbapi
from neutron.db.loadbalancer import loadbalancer_db
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common import importutils
from neutron.openstack.common import rpc
from neutron.openstack.common.rpc import dispatcher
from neutron.openstack.common.rpc import proxy
from neutron.plugins.common import constants
#from neutron.services.loadbalancer.drivers.haproxy.plugin_driver import LoadBalancerCallbacks as LoadBalancerCallbacks_base
from neutron.services.loadbalancer.drivers.haproxy.plugin_driver import LoadBalancerAgentApi
from neutron.services.loadbalancer.drivers import abstract_driver

LOG = logging.getLogger(__name__)

ACTIVE_PENDING = (
    constants.ACTIVE,
    constants.INACTIVE,
    constants.PENDING_CREATE,
    constants.PENDING_UPDATE
)

# topic name for this particular agent implementation
TOPIC_PROCESS_ON_HOST = 'q-lbaas-process-on-host'
TOPIC_LOADBALANCER_AGENT = 'lbaas_process_on_host_agent'


class LoadBalancerCallbacks(object):
    RPC_API_VERSION = '1.0'

    def __init__(self, plugin):
        self.plugin = plugin

    def create_rpc_dispatcher(self):
        return q_rpc.PluginRpcDispatcher(
            [self, agents_db.AgentExtRpcCallback(self.plugin)])

    def get_all_vips(self, context):
        with context.session.begin(subtransactions=True):
            qry = context.session.query(loadbalancer_db.Vip)
            return [(v.id, v.pool_id) for v in qry.all()]

    def get_pool_info(self, context, pool_id, host=None):
        with context.session.begin(subtransactions=True):
            qry = context.session.query(loadbalancer_db.Pool)
            qry = qry.filter_by(id=pool_id)
            pool = qry.one()

            if not pool.vip:
                raise Exception(_('Pool has no vip'))
            if (pool.status not in ACTIVE_PENDING
                or pool.vip.status not in ACTIVE_PENDING):
                raise Exception(_('Pool or vip has bad status'))
            router = self.plugin._get_resource_router_id_binding(
                context, loadbalancer_db.Vip, pool.vip.id)
            retval = {}
            retval['pool'] = self.plugin._make_pool_dict(pool)
            retval['vip'] = self.plugin._make_vip_dict(pool.vip)
            retval['members'] = [
                self.plugin._make_member_dict(m)
                for m in pool.members if m.status in ACTIVE_PENDING
            ]
            retval['healthmonitors'] = [
                self.plugin._make_health_monitor_dict(hm.monitor)
                for hm in pool.monitors
                if (hm.monitor.status == constants.ACTIVE and
                    hm.monitor.admin_state_up)
            ]

            if router:
                retval['router_id'] = router['router_id']
            return retval

    def set_pool_status(self, context, pool_id, activate=True):
        with context.session.begin(subtransactions=True):
            qry = context.session.query(loadbalancer_db.Pool)
            qry = qry.filter_by(id=pool_id)
            pool = qry.one()

            if activate:
                set_status = constants.ACTIVE
            else:
                set_status = constants.INACTIVE

            if pool.status in ACTIVE_PENDING:
                pool.status = set_status

            if pool.vip is not None and pool.vip.status in ACTIVE_PENDING:
                pool.vip.status = set_status

            for m in pool.members:
                if m.status in ACTIVE_PENDING:
                    m.status = set_status


class LVSOnHostPluginDriver(abstract_driver.LoadBalancerAbstractDriver):

    def __init__(self, plugin):
        self.agent_rpc = LoadBalancerAgentApi(topics.L3_AGENT)
        self.callbacks = LoadBalancerCallbacks(plugin)

        self.conn = rpc.create_connection(new=True)
        self.conn.create_consumer(
            TOPIC_PROCESS_ON_HOST,
            self.callbacks.create_rpc_dispatcher(),
            fanout=False)
        self.conn.consume_in_thread()
        self.plugin = plugin
        self.plugin.agent_notifiers.update(
            {q_const.AGENT_TYPE_LOADBALANCER: self.agent_rpc})

    def get_pool_agent(self, context, pool_id):
        router = self.plugin._get_resource_router_id_binding(
            context, loadbalancer_db.Pool, pool_id)
        router_id = router['router_id']
        plugin = manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)
        l3_agents = plugin.get_l3_agents_hosting_routers(
            context, [router_id])
        if not l3_agents:
            return None
        return l3_agents[0].host

    def create_vip(self, context, vip):
        agent = self.get_pool_agent(context, vip['pool_id'])
        self.agent_rpc.reload_pool(context, vip['pool_id'], agent)

    def update_vip(self, context, old_vip, vip):
        agent = self.get_pool_agent(context, vip['pool_id'])
        if (old_vip['pool_id'] != vip['pool_id'] or
            vip['status'] not in ACTIVE_PENDING) and \
           old_vip['status'] in ACTIVE_PENDING:
            self.agent_rpc.destroy_pool(context, old_vip['pool_id'],
                                        agent)
        if vip['status'] in ACTIVE_PENDING:
            self.agent_rpc.reload_pool(context, vip['pool_id'], agent)

    def delete_vip(self, context, vip):
        self.plugin._delete_db_vip(context, vip['id'])
        agent = self.get_pool_agent(context, vip['pool_id'])
        self.agent_rpc.destroy_pool(context, vip['pool_id'], agent)

    def create_pool(self, context, pool):
        # don't notify here because a pool needs a vip to be useful
        pass

    def update_pool(self, context, old_pool, pool):
        agent = self.get_pool_agent(context, pool['id'])
        if pool['status'] in ACTIVE_PENDING:
            if pool['vip_id'] is not None:
                self.agent_rpc.reload_pool(context, pool['id'], agent)
        else:
            self.agent_rpc.destroy_pool(context, pool['id'], agent)

    def delete_pool(self, context, pool):
        agent = self.get_pool_agent(context, pool['id'])
        if agent:
            self.agent_rpc.destroy_pool(context, pool['id'],
                                        agent)
        self.plugin._delete_db_pool(context, pool['id'])

    def create_member(self, context, member):
        agent = self.get_pool_agent(context, member['pool_id'])
        self.agent_rpc.modify_pool(context, member['pool_id'], agent)

    def update_member(self, context, old_member, member):
        # member may change pool id
        if member['pool_id'] != old_member['pool_id']:
            agent = self.get_pool_agent(context, old_member['pool_id'])
            if agent:
                self.agent_rpc.modify_pool(context,
                                           old_member['pool_id'],
                                           agent)
        agent = self.get_pool_agent(context, member['pool_id'])
        self.agent_rpc.modify_pool(context, member['pool_id'], agent)

    def delete_member(self, context, member):
        self.plugin._delete_db_member(context, member['id'])
        agent = self.get_pool_agent(context, member['pool_id'])
        self.agent_rpc.modify_pool(context, member['pool_id'], agent)

    def update_health_monitor(self, context, old_health_monitor,
                              health_monitor, pool_id):
        # monitors are unused here because agent will fetch what is necessary
        agent = self.get_pool_agent(context, pool_id)
        self.agent_rpc.modify_pool(context, pool_id, agent)

    def create_pool_health_monitor(self, context, healthmon, pool_id):
        # healthmon is not used here
        agent = self.get_pool_agent(context, pool_id)
        self.agent_rpc.modify_pool(context, pool_id, agent)

    def delete_pool_health_monitor(self, context, health_monitor, pool_id):
        self.plugin._delete_db_pool_health_monitor(
            context, health_monitor['id'], pool_id
        )

        # healthmon_id is not used here
        agent = self.get_pool_agent(context, pool_id)
        self.agent_rpc.modify_pool(context, pool_id, agent)

    def stats(self, context, pool_id):
        pass
