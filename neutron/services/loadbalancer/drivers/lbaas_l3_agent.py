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

from neutron.openstack.common import importutils

OPTS = [
    cfg.StrOpt('l3_agent_lbaas_driver',
               default=['neutron.services.loadbalancer.drivers.lvs.'
                        'agent.LBaaSL3AgentDriver'],
               help=_('The LBaaS drivers loaded by the L3 agent')),
]

class LBaaSL3Agent(object):

    def __init__(self, conf):
        super(LBaaSL3Agent, self).__init__(conf=conf)
        self._load_drivers(conf)

    def _load_drivers(self, conf):
        self.lbaas_drivers = []
        for d in conf.l3_agent_lbaas_driver:
            self.lbaas_drivers.append(importutils.import_object(
                d, conf, self))

    def _process_routers(self, routers, all_routers=False):
        for d in self.lbaas_drivers:
            d._process_routers(routers, all_routers)
