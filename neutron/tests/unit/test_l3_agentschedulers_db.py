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

import mock

from neutron.db import l3_agentschedulers_db
from neutron.tests import base
from neutron.openstack.common import uuidutils
from neutron.tests.unit import test_l3_plugin

_uuid = uuidutils.generate_uuid

class TestL3AgentSchedulerDbMixin(base.BaseTestCase, test_l3_plugin.L3NatTestCaseMixin):

    def setUp(self):
        super(TestL3AgentSchedulerDbMixin, self).setUp()
        self.agentscheduler = l3_agentschedulers_db.L3AgentSchedulerDbMixin()

    def tearDown(self):
        super(TestL3AgentSchedulerDbMixin, self).tearDown()

    def test_list_active_sync_routers_on_active_l3_agent(self):
        
        router_ids = [_uuid()]
        routers = self.agentscheduler.list_active_sync_routers_on_active_l3_agent(mock.Mock(), router_ids, active=True)
        if r in routers:
            self.assertEqual(r['router']['id'], router_ids)
