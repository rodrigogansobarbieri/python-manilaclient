# Copyright 2015 Mirantis Inc.
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

import ddt

from tempest.lib.common.utils import data_utils

from manilaclient.common import constants
from manilaclient import config
from manilaclient.tests.functional import base
from manilaclient.tests.functional import utils

CONF = config.CONF


class SharesReadWriteBase(base.BaseTestCase):
    protocol = None

    @classmethod
    def setUpClass(cls):
        super(SharesReadWriteBase, cls).setUpClass()
        if cls.protocol not in CONF.enable_protocols:
            message = "%s tests are disabled" % cls.protocol
            raise cls.skipException(message)
        cls.name = data_utils.rand_name('autotest_share_name')
        cls.description = data_utils.rand_name('autotest_share_description')

        # NOTE(vponomaryov): following share is used only in one test
        # until tests for snapshots appear.
        cls.share = cls.create_share(
            share_protocol=cls.protocol,
            size=1,
            name=cls.name,
            description=cls.description,
            client=cls.get_user_client(),
            cleanup_in_class=True)

    def test_create_delete_share(self):
        name = data_utils.rand_name('autotest_share_name')

        create = self.create_share(
            self.protocol, name=name, client=self.user_client)

        self.assertEqual(name, create['name'])
        self.assertEqual('1', create['size'])
        self.assertEqual(self.protocol.upper(), create['share_proto'])

        self.user_client.delete_share(create['id'])

        self.user_client.wait_for_share_deletion(create['id'])

    def test_create_update_share(self):
        name = data_utils.rand_name('autotest_share_name')
        new_name = 'new_' + name
        description = data_utils.rand_name('autotest_share_description')
        new_description = 'new_' + description

        create = self.create_share(
            self.protocol, name=name, description=description,
            client=self.user_client)

        self.assertEqual(name, create['name'])
        self.assertEqual(description, create['description'])
        self.assertEqual('False', create['is_public'])

        self.user_client.update_share(
            create['id'], new_name, new_description, True)
        get = self.user_client.get_share(create['id'])

        self.assertEqual(new_name, get['name'])
        self.assertEqual(new_description, get['description'])
        self.assertEqual('True', get['is_public'])

    def test_get_share(self):
        get = self.user_client.get_share(self.share['id'])

        self.assertEqual(self.name, get['name'])
        self.assertEqual(self.description, get['description'])
        self.assertEqual('1', get['size'])
        self.assertEqual(self.protocol.upper(), get['share_proto'])
        self.assertTrue(get.get('export_locations', []) > 0)


@ddt.ddt
class SharesTestMigration(base.BaseTestCase):

    @classmethod
    def setUpClass(cls):
        super(SharesTestMigration, cls).setUpClass()

        cls.old_type = cls.create_share_type(
            data_utils.rand_name('autotest_share_name'),
            driver_handles_share_servers=True)
        cls.new_type = cls.create_share_type(
            data_utils.rand_name('autotest_share_name'),
            driver_handles_share_servers=True)

        cls.old_share_net = cls.get_share_network(
            cls.get_user_client().share_network)
        cls.new_share_net = cls.create_share_network(
            neutron_net_id=cls.old_share_net['neutron_net_id'],
            neutron_subnet_id=cls.old_share_net['neutron_subnet_id'])

    @utils.skip_if_microversion_not_supported('2.22')
    @ddt.data('migration_error', 'migration_success', 'None')
    def test_reset_task_state(self, state):

        share = self.create_share(
            share_protocol='nfs',
            size=1,
            name=data_utils.rand_name('autotest_share_name'),
            client=self.get_user_client(),
            cleanup_in_class=True,
            share_type=self.old_type['ID'],
            share_network=self.old_share_net,
            wait_for_creation=True)

        self.admin_client.reset_task_state(share['id'], state)
        share = self.admin_client.get_share(share['id'])
        self.assertEqual(state, share['task_state'])

    @utils.skip_if_microversion_not_supported('2.26')
    @ddt.data('cancel', 'success', 'error')
    def test_full_migration(self, test_type):
        # We are testing with DHSS=True only because it allows us to specify
        # new_share_network.

        share = self.create_share(
            share_protocol='nfs',
            size=1,
            name=data_utils.rand_name('autotest_share_name'),
            client=self.get_user_client(),
            cleanup_in_class=True,
            share_type=self.old_type['ID'],
            share_network=self.old_share_net,
            wait_for_creation=True)

        pools = self.admin_client.pool_list(detail=True)

        dest_pool = utils.choose_matching_backend(
            share, pools, self.new_type)

        self.assertIsNotNone(dest_pool)

        source_pool = share['host']

        self.admin_client.migration_start(
            share['id'], dest_pool, True, True, True, True, False,
            self.new_share_net['id'], self.new_type['ID'])

        if test_type == 'error':
            statuses = constants.TASK_STATE_MIGRATION_ERROR
        else:
            statuses = (constants.TASK_STATE_MIGRATION_DRIVER_PHASE1_DONE,
                        constants.TASK_STATE_DATA_COPYING_COMPLETED)

        share = self.admin_client.wait_for_migration_status(
            share['id'], dest_pool, statuses)

        progress = self.admin_client.migration_get_progress(share['id'])
        self.assertEqual('100', progress['total_progress'])

        self.assertEqual(source_pool, share['host'])
        self.assertEqual(self.old_type['ID'], share['share-type'])
        self.assertEqual(self.old_share_net['ID'], share['share_network_id'])

        if test_type == 'success':
            self.admin_client.migration_complete(share['id'])
            statuses = constants.TASK_STATE_MIGRATION_SUCCESS
        elif test_type == 'cancel':
            self.admin_client.migration_cancel(share['id'])
            statuses = constants.TASK_STATE_MIGRATION_CANCELLED
        else:
            # Error scenario is complete
            self.assertEqual(statuses, progress['task_state'])
            return

        share = self.admin_client.wait_for_migration_status(
            share['id'], dest_pool, statuses)

        progress = self.admin_client.migration_get_progress(share['id'])
        self.assertEqual(statuses, progress['task_state'])

        if test_type == 'success':
            self.assertEqual(dest_pool, share['host'])
            self.assertEqual(self.new_type['ID'], share['share-type'])
            self.assertEqual(self.new_share_net['ID'],
                             share['share_network_id'])

        else:
            self.assertEqual(source_pool, share['host'])
            self.assertEqual(self.old_type['ID'], share['share-type'])
            self.assertEqual(self.old_share_net['ID'],
                             share['share_network_id'])


class NFSSharesReadWriteTest(SharesReadWriteBase):
    protocol = 'nfs'


class CIFSSharesReadWriteTest(SharesReadWriteBase):
    protocol = 'cifs'


class GlusterFSSharesReadWriteTest(SharesReadWriteBase):
    protocol = 'glusterfs'


class HDFSSharesReadWriteTest(SharesReadWriteBase):
    protocol = 'hdfs'
