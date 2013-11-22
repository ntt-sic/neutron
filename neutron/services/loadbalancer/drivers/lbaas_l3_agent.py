# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: IWAMOTO Toshihiro, VA Linux Systems Japan

from oslo.config import cfg
import netaddr

import neutron.agent.l3_agent
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron import context
from neutron.openstack.common import log as logging
from neutron.openstack.common.rpc import proxy
import os

LOG = logging.getLogger(__name__)

OPTS = [
    cfg.StrOpt('loadbalancer_confs',
               default='$state_path/lb',
               help=_('Location to store loadbalancer config files')),
]

class LBaaSL3AgentApi(proxy.RpcProxy):

    RPC_API_VERSION = '1.0'

    def __init__(self, topic, context, host):
        super(LBaaSL3AgentApi, self).__init__(topic, self.RPC_API_VERSION)
        self.context = context
        self.host = host

    def get_router_pools(self, router_id):
        return self.call(
            self.context,
            self.make_msg('get_router_pools', router_id=router_id,
                          host=self.host),
            topic=self.topic
        )

    def get_pool_info(self, pool_id):
        return self.call(
            self.context,
            self.make_msg('get_pool_info', pool_id=pool_id, host=self.host),
            topic=self.topic
        )

    def set_pool_status(self, pool_id, activate=True):
        return self.call(
            self.context,
            self.make_msg('set_pool_status', pool_id=pool_id,
                          activate=activate, host=self.host),
            topic=self.topic
        )


class LBaaSL3AgentRpcCallback(object):

    def __init__(self, conf):
        LOG.debug(_("Initializing loadbalancer agent"))
        self.conf = conf

        self.lb_conf_dir = os.path.abspath(
            os.path.normpath(conf.loadbalancer_confs))
        if not os.path.isdir(self.lb_conf_dir):
            os.makedirs(self.lb_conf_dir, 0o755)
        self.lb_state_cache = {}

        # TODO: import vendor specific code into self.lbaas_driver
        self.context = context.get_admin_context_without_session()
        self.lbaas_rpc = LBaaSL3AgentApi('q-lbaas-process-on-host',
                                         self.context, conf.host)

        super(LBaaSL3AgentRpcCallback, self).__init__(conf=conf)

    def _get_value_from_conf_file(self, file, converter=None):                  
        """A helper function to read a value from one of the state files."""    
        msg = _('Error while reading %s')

        try:
            with open(file, 'r') as f:
                try:
                    return converter and converter(f.read()) or f.read()
                except ValueError:
                    msg = _('Unable to convert value in %s')
        except IOError:
            msg = _('Unable to access %s')

        LOG.debug(msg % file)
        return None
                                                                                
    def is_keepalived_alive(self, pid):
        try:
            with open('/proc/%d/cmdline' % pid, 'r') as f:
                c = f.read()
                if c.startswith('keepalived'):
                    return True
        except IOError:
            pass
        except Exception:
            LOG.debug(_("Unexpected error in is_keepalived_alive: ") +
                      str(sys.exc_info()))
        return False

    def create_keepalived_conf(self, pool_id, pool_info):
        """Create a keepalived.conf.
        Returns False if no changes are made to the config file."""
        cfname = os.path.join(self.lb_conf_dir, pool_id)
        lb_active = pool_info['vip']['admin_state_up'] and \
            pool_info['pool']['admin_state_up']

        vip = pool_info['vip']
        buf = "virtual_server %s %d\n{\n" % (vip['address'],
                                             vip['protocol_port'])

        if len(pool_info['healthmonitors']):
            buf += "delay_loop %d\n" % \
                   min(map(lambda x: x['delay'],
                           pool_info['health_monitors']))

        lb_algo_map = {'ROUND_ROBIN': 'wrr',
                       'LEAST_CONNECTIONS': 'wlc'}
        buf += "lb_algo %s\nlb_kind NAT\n" % \
                  lb_algo_map[pool_info['pool']['lb_method']]
        buf += "protocol %s\n" % pool_info['pool']['protocol']

        for m in pool_info['members']:
            if lb_active and m['admin_state_up']:
                weight = m['weight']
            else:
                weight = 0
            buf += "real_server %s %d\n    {\n\tweight %d\n" % \
                   (m['address'], m['protocol_port'], weight)
            buf += "\tinhibit_on_failure\n"
            if vip['connection_limit'] > 0:
                buf += "\tuthreshold %d\n" % m['connection_limit']
            # TODO: add notify_{up,down}
            for h in pool_info['healthmonitors']:
                buf += "\tMISC_CHECK\n\t{\n\t    misc_path \""
                if h['type'] == 'TCP':
                    buf += self.conf.script_check_tcp
                elif h['type'] == 'PING':
                    buf += self.conf.script_check_ping
                else:
                    LOG.error("Invalid health monitor type '%s'" %
                              h['type'])
                    raise ValueError(h['type'])
                buf += " %d %d %s" % \
                          (h['timeout'], h['max_retries'], m['address'])
                if h['type'] == 'TCP':
                    addr = pool_info['vip']['rip_address']
                    buf += " %d %s\"\n" % (m['protocol_port'], addr)

                else:
                    buf += " \"\n"
                buf += "\t    misc_timeout %d\n\t}\n" % \
                          (h['timeout'] * (h['max_retries'] + 1))
            buf += "    }\n"
        buf += "}\n"
        try:
            with open(cfname, 'r') as f:
                if f.read() == buf:
                    return False
        except:
            pass
        utils.replace_file(cfname, buf)
        return True

    def enable_keepalived(self, pool_id, ns_name, force_reload=False):
        """If a keepalived is already running for the specified pool,
        sends a SIGHUP to reload its config file.
        Otherwise, starts a keepalived."""
        cfname = os.path.join(self.lb_conf_dir, pool_id)
        pidname = {}
        pidname['main'] = cfname + '.pid'
        pidname['checker'] = cfname + '-checker.pid'
        pid = {}

        for t in ['main', 'checker']:
            pid[t] = self._get_value_from_conf_file(pidname[t], int)
            if pid[t]:
                if not self.is_keepalived_alive(pid[t]):
                    pid[t] = None
                    utils.execute(['rm', '-f', pidname[t]],
                                  self.conf.root_helper)
        if pid['main'] is None and pid['checker']:
            utils.execute(['kill', '-15', pid['checker']], self.conf.root_helper)
        elif pid['main'] and pid['checker'] is None:
            utils.execute(['kill', '-15', pid['main']], self.conf.root_helper)
            pid['main'] = None

        if pid['main']:
            if not force_reload:
                return
            cmd = ['kill', '-HUP', pid['main']]
            utils.execute(cmd, self.conf.root_helper)
            return
        ip_wrapper_root = ip_lib.IPWrapper(self.conf.root_helper)
        ip_wrapper = ip_wrapper_root.ensure_namespace(ns_name)
        ip_wrapper.netns.execute(
            ['keepalived', '-f', cfname, '-p', pidname['main'],
             '-C', '-c', pidname['checker']])

    def disable_keepalived(self, pool_id):
        """Stop a keepalived and remove its config file."""
        cfname = os.path.join(self.lb_conf_dir, pool_id)
        pid = self._get_value_from_conf_file(cfname + '.pid', int)
        checker_pid = self._get_value_from_conf_file(
            cfname + '-checker.pid', int)

        if pid:
            cmd = ['kill', '-15', pid]
            utils.execute(cmd, self.conf.root_helper)
            self.keepalived_to_kill.append(pid)
        if checker_pid:
            self.keepalived_to_kill.append(checker_pid)

        os.unlink(cfname)

    def _reload_pool(self, pool_id=None, host=None):
        pool_info = self.lbaas_rpc.get_pool_info(pool_id)
        self.lb_state_cache[pool_id] = pool_info
        ri = self.router_info[pool_info['router_id']]
        # TODO: support multiple vips per router
        
        # assign VIP IP
        ifname = None
        for p in ri.internal_ports:
            if p['subnet_id'] == pool_info['vip']['subnet_id']:
                ifname = self.get_external_device_name(p['id'])
                break
        if ifname is None and ri.router['gw_port']:
            if ri.router['gw_port']['subnet_id'] == \
               pool_info['vip']['subnet_id']:
                ifname = self.get_external_device_name(ri.router['gw_port_id'])
        
        device = ip_lib.IPDevice(ifname, self.root_helper,
                                 namespace=ri.ns_name())
        vip_cidr = pool_info['vip']['address'] + '/32'
        if vip_cidr not in [addr['cidr'] for addr in device.addr.list()]:
            net = netaddr.IPNetwork(vip_cidr)
            device.addr.add(net.version, vip_cidr, str(net.broadcast))

        changed = self.create_keepalived_conf(pool_id, pool_info)
        self.enable_keepalived(pool_id, ri.ns_name())

    def process_router_add(self, ri):
        LOG.debug(_("Lbaasl3agentrpccallback process_router_add for %s called")
                  % ri.router['id'])
        pools = self.lbaas_rpc.get_router_pools(ri.router['id'])
        for p in pools:
            _reload_pool(pool_id=p)

        super(LBaaSL3AgentRpcCallback, self).process_router_add(ri)

    def reload_pool(self, context, pool_id=None, host=None):
        LOG.debug(_("reload_pool called"))
        self._reload_pool(pool_id=pool_id, host=host)

    def modify_pool(self, context, pool_id=None, host=None):
        LOG.debug(_("modify_pool called"))
        self._reload_pool(context, pool_id=pool_id, host=host)

    def destroy_pool(self, context, pool_id=None, host=None):
        LOG.debug(_("destroy_pool called"))

        pool_info = self.lb_state_cache[pool_id]
        routers = self.plugin_rpc.get_routers(
            context, [pool_info['router_id']])
        ri = neutron.agent.l3_agent.RouterInfo(pool_info['router_id'],
                                               self.root_helper,
                                               self.conf.use_namespaces,
                                               routers[0])

        self.disable_keepalived(pool_id, ri.ns_name())

        ifname = self.get_external_device_name(ri.router['gw_port_id'])
        device = ip_lib.IPDevice(ifname, self.root_helper,
                                 namespace=ri.ns_name())
        vip_cidr = pool_info['vip']['address'] + '/32'
        net = netaddr.IPNetwork(vip_cidr)
        device.addr.delete(net.version, vip_cidr)
        del self.lb_state_cache[pool_id]
