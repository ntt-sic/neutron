# Copyright (c) 2013 OpenStack Foundation.
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

import contextlib
import mock

from neutron.api import extensions
from neutron.db import l3_agentschedulers_db
from neutron.tests import base
from neutron.openstack.common import uuidutils
from neutron.tests.unit import test_l3_plugin
from neutron.tests.unit import test_db_plugin
from neutron.tests.unit import test_agent_ext_plugin
from neutron.tests.unit import test_extensions

_uuid = uuidutils.generate_uuid

class TestL3AgentSchedulerDbMixin(test_l3_plugin.L3NatTestCaseMixin,
                                  test_agent_ext_plugin.AgentDBTestMixIn,
                                  test_db_plugin.NeutronDbPluginV2TestCase):

    fmt = 'json'
    def setUp(self):
        super(TestL3AgentSchedulerDbMixin, self).setUp()
        self.agentscheduler = l3_agentschedulers_db.L3AgentSchedulerDbMixin()
        ext_mgr = extensions.PluginAwareExtensionManager.get_instance()
        self.ext_api = test_extensions.setup_extensions_middleware(ext_mgr)

    def tearDown(self):
        super(TestL3AgentSchedulerDbMixin, self).tearDown()

    def test_list_active_sync_routers_on_active_l3_agent(self):
        with contextlib.nested(self.router(), self.router()) as routers:
            router_ids = [r['router']['id'] for r in routers]
            ret_a = self.agentscheduler.list_active_sync_routers_on_active_l3_agent(
                mock.Mock(), router_ids, active=True)
            self.assertEqual(set(router_ids), set([r['id'] for r in ret_a]))
        """    
        router_ids = [_uuid()]
        routers = self.agentscheduler.list_active_sync_routers_on_active_l3_agent(
                mock.Mock(), router_ids, active=True)
        if r in routers:
            self.assertEqual(r['router']['id'], router_ids)
        """
