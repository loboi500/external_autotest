# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from autotest_lib.server import utils

AUTHOR = 'dbeckett'
NAME = 'policy_ArcExternalStorageDisabled.Arc'
TIME = 'SHORT'
TEST_CATEGORY = 'General'
TEST_CLASS = 'enterprise'
TEST_TYPE = 'server'
ATTRIBUTES = 'suite:ent-nightly, suite:policy'
DEPENDENCIES = 'servo_state:WORKING'

DOC = """
This test connects the servo repair USB stick to the DUT, then runs the
client-side tests for the policy_ArcExternalStorageDisabled. At the end of the
test it disconnects the USB stick.

"""

args_dict = utils.args_to_dict(args)
servo_args = hosts.CrosHost.get_servo_arguments(args_dict)

def run(machine):
    host = hosts.create_host(machine, servo_args=servo_args)
    job.run_test('policy_ExternalStorageServer',
                 host=host,
                 client_test='policy_ArcExternalStorageDisabled')

parallel_simple(run, machines)
