#!/usr/bin/env python2
# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""library functions to prepare a DUT for lab deployment.

This library will be shared between Autotest and Skylab DUT deployment tools.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import time

import common
import logging
from autotest_lib.client.common_lib import error
from autotest_lib.client.common_lib import utils
from autotest_lib.server import hosts
from autotest_lib.server import site_utils as server_utils
from autotest_lib.server.hosts import host_info
from autotest_lib.server.hosts import servo_host
from autotest_lib.server.hosts import cros_constants
from autotest_lib.server.hosts import servo_constants


_FIRMWARE_UPDATE_TIMEOUT = 600
# Check battery level with retries.
# If battery level is low then sleep to 15 minutes.
_BATTERY_LEVEL_CHECK_RETRIES = 8
_BATTERY_LEVEL_CHECK_RETRIES_TIMEOUT = 900
# We expecting that battery will change more than 4% for 15 minutes.
_BATTERY_LEVEL_CHANGE_IN_ONE_RETRY = 4


@contextlib.contextmanager
def create_cros_host(hostname, board, model, servo_hostname, servo_port,
                servo_serial=None, logs_dir=None):
    """Yield a server.hosts.CrosHost object to use for DUT preparation.

    This object contains just enough inventory data to be able to prepare the
    DUT for lab deployment. It does not contain any reference to AFE / Skylab so
    that DUT preparation is guaranteed to be isolated from the scheduling
    infrastructure.

    @param hostname:        FQDN of the host to prepare.
    @param board:           The autotest board label for the DUT.
    @param model:           The autotest model label for the DUT.
    @param servo_hostname:  FQDN of the servo host controlling the DUT.
    @param servo_port:      Servo host port used for the controlling servo.
    @param servo_serial:    (Optional) Serial number of the controlling servo.
    @param logs_dir:        (Optional) Directory to save logs obtained from the
                            host.

    @yield a server.hosts.Host object.
    """
    labels = [
            'board:%s' % board,
            'model:%s' % model,
    ]
    attributes = {
            servo_constants.SERVO_HOST_ATTR: servo_hostname,
            servo_constants.SERVO_PORT_ATTR: servo_port,
    }
    if servo_serial is not None:
        attributes[servo_constants.SERVO_SERIAL_ATTR] = servo_serial

    store = host_info.InMemoryHostInfoStore(info=host_info.HostInfo(
            labels=labels,
            attributes=attributes,
    ))
    machine_dict = _get_machine_dict(hostname, store)
    host = hosts.create_host(machine_dict)
    servohost = servo_host.ServoHost(
            **servo_host.get_servo_args_for_host(host))
    _prepare_servo(servohost)
    host.set_servo_host(servohost)
    host.servo.uart_logs_dir = logs_dir
    try:
        yield host
    finally:
        host.close()


def _get_machine_dict(hostname, host_info_store):
    """Helper function to generate a machine_dic to feed hosts.create_host.

    @param hostname
    @param host_info_store

    @return A dict that hosts.create_host can consume.
    """
    return {'hostname': hostname,
            'host_info_store': host_info_store,
            'afe_host': server_utils.EmptyAFEHost(),
            }


def download_image_to_servo_usb(host, build):
    """Download the given image to the USB attached to host's servo.

    @param host   A server.hosts.Host object.
    @param build  A Chrome OS version string for the build to download.
    """
    _, update_url = host.stage_image_for_servo(build)
    host.servo.image_to_servo_usb(update_url)


def try_reset_by_servo(host):
    """Reboot the DUT by run cold_reset by servo.

    Cold reset implemented as
    `dut-control -p <SERVO-PORT> power_state:reset`.

    @params host: CrosHost instance with initialized servo instance.
    """
    logging.info('Attempting reset via servo...')
    host.servo.get_power_state_controller().reset()

    logging.info('Waiting for DUT to come back up.')
    if not host.wait_up(timeout=host.BOOT_TIMEOUT):
        raise error.AutoservError(
            'DUT failed to come back after %d seconds' % host.BOOT_TIMEOUT)


def power_cycle_via_servo(host, recover_src=False):
    """Power cycle a host though it's attached servo.

    @param host: A server.hosts.Host object.
    @param recover_src: Indicate if we need switch servo_v4_role
           back to src mode.
    """
    try:
        logging.info('Shutting down %s from via ssh.', host.hostname)
        host.halt()
    except Exception as e:
        logging.info('Unable to shutdown DUT via ssh; %s', str(e))

    if recover_src:
        host.servo.set_servo_v4_role('src')

    logging.info('Power cycling DUT through servo...')
    host.servo.get_power_state_controller().power_off()
    host.servo.switch_usbkey('off')
    time.sleep(host.SHUTDOWN_TIMEOUT)
    # N.B. The Servo API requires that we use power_on() here
    # for two reasons:
    #  1) After turning on a DUT in recovery mode, you must turn
    #     it off and then on with power_on() once more to
    #     disable recovery mode (this is a Parrot specific
    #     requirement).
    #  2) After power_off(), the only way to turn on is with
    #     power_on() (this is a Storm specific requirement).
    time.sleep(host.SHUTDOWN_TIMEOUT)
    host.servo.get_power_state_controller().power_on()

    logging.info('Waiting for DUT to come back up.')
    if not host.wait_up(timeout=host.BOOT_TIMEOUT):
        raise error.AutoservError('DUT failed to come back after %d seconds' %
                                  host.BOOT_TIMEOUT)


def verify_battery_status(host):
    """Verify that battery status.

    If DUT battery still in the factory mode then DUT required re-work.

    @param host server.hosts.CrosHost object.
    @raise Exception: if status as unexpected value.
    """
    logging.info("Started to verify battery status")
    host_info = host.host_info_store.get()
    if host_info.get_label_value('power') != 'battery':
        logging.info("Skepping due DUT does not have the battery")
        return
    power_info = host.get_power_supply_info()
    battery_path = power_info['Battery']['path']
    cmd = 'cat %s/status' % battery_path
    status = host.run(cmd, timeout=30, ignore_status=True).stdout.strip()
    if status not in ['Charging', 'Discharging', 'Full']:
        raise Exception(
                'Unexpected battery status. Please verify that DUT prepared'
                ' for deployment.')

    # Verify battery level to avoid cases when DUT in factory mode which can
    # block battery from charging. Retry check will take 8 attempts by
    # 15 minutes to allow battery to reach required level.
    battery_level_good = False
    last_battery_level = 0
    for _ in range(_BATTERY_LEVEL_CHECK_RETRIES):
        power_info = host.get_power_supply_info()
        battery_level = float(power_info['Battery']['percentage'])
        # Verify if battery reached the required level
        battery_level_good = battery_level >= cros_constants.MIN_BATTERY_LEVEL
        if battery_level_good:
            # Stop retry as battery reached the required level
            break
        logging.info(
                'Battery level %s%% is lower than expected %s%%.'
                ' Sleep for %s seconds to try again', battery_level,
                cros_constants.MIN_BATTERY_LEVEL,
                _BATTERY_LEVEL_CHECK_RETRIES_TIMEOUT)
        time.sleep(_BATTERY_LEVEL_CHECK_RETRIES_TIMEOUT)

        if last_battery_level > 0:
            # If level of battery is changing less than 4% per 15 minutes
            # then we can assume that the battery is not charging as expected
            # or stuck on some level.
            battery_level_change = abs(last_battery_level - battery_level)
            if battery_level_change < _BATTERY_LEVEL_CHANGE_IN_ONE_RETRY:
                logging.info(
                        'Battery charged less than 4%% for 15 minutes which'
                        ' means that something wrong with charging.'
                        ' Stop retry to charge it. Battery level: %s%%',
                        battery_level)
                break
        last_battery_level = battery_level
    if not battery_level_good:
        raise Exception(
                'Battery is not charged or discharging.'
                ' Please verify that DUT connected to power and charging.'
                ' Possible that the DUT is not ready for deployment in lab.')
    logging.info("Battery status verification passed!")


def verify_servo(host):
    """Verify that we have good Servo.

    The servo_topology and servo_type will be clean up when initiate the
    deploy process by run add-dut or update-dut.
    """
    host_info = host.host_info_store.get()
    if host_info.os == 'labstation':
        # skip labstation because they do not have servo
        return
    servo_host = host._servo_host
    if not servo_host:
        raise Exception('Servo host is not initialized. All DUTs need to have'
                        ' a stable and working servo.')
    if host._servo_host.is_servo_topology_supported():
        servo_topology = host._servo_host.get_topology()
        if not servo_topology or servo_topology.is_empty():
            raise Exception(
                    'Servo topology is not initialized. All DUTs need to have'
                    ' a stable and working servo.')
    servo_type = host.servo.get_servo_type()
    if not servo_type:
        raise Exception(
                'The servo_type did not received from Servo. Please verify'
                ' that Servo is in good state. All DUTs need to have a stable'
                ' and working servo.')
    if not host.is_servo_in_working_state():
        raise Exception(
                'Servo is not initialized properly or did not passed one or'
                ' more verifiers. All DUTs need to have a stable and working'
                ' servo.')
    host._set_servo_topology()
    logging.info("Servo initialized and working as expected.")


def verify_ccd_testlab_enable(host):
    """Verify that ccd testlab enable when DUT support cr50.

    The new deploy process required to deploy DUTs with testlab enable when
    connection to the servo by type-c, so we will be sure that communication
    by servo is permanent, it's critical for auto-repair capability.

    @param host server.hosts.CrosHost object.
    """

    host_info = host.host_info_store.get()
    if host_info.os == 'labstation':
        # skip labstation because they do not has servo
        return

    # Only verify for ccd servo connection
    if host.servo and host.servo.get_main_servo_device() == 'ccd_cr50':
        if not host.servo.has_control('cr50_testlab'):
            raise Exception(
                'CCD connection required support of cr50 on the DUT. Please '
                'verify which servo need to be used for DUT setup.')

        status = host.servo.get('cr50_testlab')
        if status == 'on':
            logging.info("CCD testlab mode is enabled on the DUT.")
        else:
            raise Exception(
                'CCD testlab mode is not enabled on the DUT, enable '
                'testlab mode is required for all DUTs that support CR50.')


def verify_labstation_RPM_config_unsafe(host):
    """Verify that we can power cycle a labstation with its RPM information.
    Any host without RPM information will be safely skipped.

    @param host: any host

    This procedure is intended to catch inaccurate RPM info when the
    host is deployed.

    If the RPM config information is wrong, then this command will fail.

    Note that we do not cleanly stop servod as part of power-cycling the DUT;
    therefore calling this function is not safe in general.

    """
    host_info = host.host_info_store.get()

    powerunit_hostname = host_info.attributes.get('powerunit_hostname')
    powerunit_outlet   = host_info.attributes.get('powerunit_outlet')

    powerunit_hasinfo = (bool(powerunit_hostname), bool(powerunit_outlet))

    if powerunit_hasinfo == (True, True):
        pass
    elif powerunit_hasinfo == (False, False):
        logging.info("intentionally skipping labstation %s", host.hostname)
        return
    else:
        msg = "inconsistent power info: %s %s" % (
            powerunit_hostname, powerunit_outlet
        )
        logging.error(msg)
        raise Exception(msg)

    logging.info("Shutting down labstation...")
    host.rpm_power_off_and_wait()
    host.rpm_power_on_and_wait()
    logging.info("RPM Check Successful")


def verify_boot_into_rec_mode(host):
    """Verify that we can boot into USB when in recover mode, and reset tpm.

    The new deploy process will install test image before firmware update, so
    we don't need boot into recovery mode during deploy, but we still want to
    make sure that DUT can boot into recover mode as it's critical for
    auto-repair capability.

    @param host   servers.host.Host object.
    """
    try:
        # The DUT could be start with un-sshable state, so do shutdown from
        # DUT side in a try block.
        logging.info('Shutting down %s from via ssh.', host.hostname)
        host.halt()
    except Exception as e:
        logging.info('Unable to shutdown DUT via ssh; %s', str(e))

    host.servo.get_power_state_controller().power_off()
    time.sleep(host.SHUTDOWN_TIMEOUT)
    logging.info("Booting DUT into recovery mode...")
    need_snk = host.require_snk_mode_in_recovery()
    host.servo.boot_in_recovery_mode(snk_mode=need_snk)
    try:
        if not host.wait_up(timeout=host.USB_BOOT_TIMEOUT):
            raise Exception('DUT failed to boot into recovery mode.')

        logging.info('Resetting the TPM status')
        try:
            host.run('chromeos-tpm-recovery')
        except error.AutoservRunError:
            logging.warn('chromeos-tpm-recovery is too old.')
    except Exception:
        # Restore the servo_v4 role to src if we called boot_in_recovery_mode
        # method with snk_mode=True earlier. If no exception raise, recover
        # src mode will be handled by power_cycle_via_servo() method.
        if need_snk:
            host.servo.set_servo_v4_role('src')
        raise

    logging.info("Rebooting host into normal mode.")
    power_cycle_via_servo(host, recover_src=need_snk)
    logging.info("Verify boot into recovery mode completed successfully.")


def install_test_image(host):
    """Initial install a test image on a DUT.

    This function assumes that the required image is already downloaded onto the
    USB key connected to the DUT via servo, and the DUT is in dev mode with
    dev_boot_usb enabled.

    @param host   servers.host.Host object.
    """
    servo = host.servo
    # First power on.  We sleep to allow the firmware plenty of time
    # to display the dev-mode screen; some boards take their time to
    # be ready for the ctrl+U after power on.
    servo.get_power_state_controller().power_off()
    time.sleep(host.SHUTDOWN_TIMEOUT)
    servo.switch_usbkey('dut')
    servo.get_power_state_controller().power_on()

    # Type ctrl+U repeatedly for up to BOOT_TIMEOUT or until DUT boots.
    boot_deadline = time.time() + host.BOOT_TIMEOUT
    while time.time() < boot_deadline:
        logging.info("Pressing ctrl+u")
        servo.ctrl_u()
        if host.ping_wait_up(timeout=5):
            break
    else:
        raise Exception('DUT failed to boot from USB for install test image.')

    host.run('chromeos-install --yes', timeout=host.INSTALL_TIMEOUT)

    logging.info("Rebooting DUT to boot from hard drive.")
    try:
        host.reboot()
    except Exception as e:
        logging.info('Failed to reboot DUT via ssh; %s', str(e))
        try_reset_by_servo(host)
    logging.info("Install test image completed successfully.")


def reinstall_test_image(host):
    """Install the test image of given build to DUT.

    This function assumes that the required image is already downloaded onto the
    USB key connected to the DUT via servo.

    @param host   servers.host.Host object.
    """
    host.servo_install()


def flash_firmware_using_servo(host, build):
    """Flash DUT firmware directly using servo.

    Rather than running `chromeos-firmwareupdate` on DUT, we can flash DUT
    firmware directly using servo (run command `flashrom`, etc. on servo). In
    this way, we don't require DUT to be in dev mode and with dev_boot_usb
    enabled."""
    host.firmware_install(build)


def install_firmware(host):
    """Install dev-signed firmware after removing write-protect.

    At start, it's assumed that hardware write-protect is disabled,
    the DUT is in dev mode, and the servo's USB stick already has a
    test image installed.

    The firmware is installed by powering on and typing ctrl+U on
    the keyboard in order to boot the test image from USB.  Once
    the DUT is booted, we run a series of commands to install the
    read-only firmware from the test image.  Then we clear debug
    mode, and shut down.

    @param host   Host instance to use for servo and ssh operations.
    """
    logging.info("Started install firmware on the DUT.")
    # Disable software-controlled write-protect for both FPROMs, and
    # install the RO firmware.
    for fprom in ['host', 'ec']:
        host.run('flashrom -p %s --wp-disable' % fprom,
                 ignore_status=True)

    fw_update_log = '/mnt/stateful_partition/home/root/cros-fw-update.log'
    pid = _start_firmware_update(host, fw_update_log)
    _wait_firmware_update_process(host, pid)
    _check_firmware_update_result(host, fw_update_log)

    try:
        host.reboot()
    except Exception as e:
        logging.debug('Failed to reboot the DUT after update firmware; %s', e)
        try_reset_by_servo(host)

    # Once we confirmed DUT can boot from new firmware, get us out of
    # dev-mode and clear GBB flags.  GBB flags are non-zero because
    # boot from USB was enabled.
    logging.info("Resting gbb flags and disable dev mode.")
    host.run('/usr/share/vboot/bin/set_gbb_flags.sh 0',
             ignore_status=True)
    host.run('crossystem disable_dev_request=1',
             ignore_status=True)

    logging.info("Rebooting DUT in normal mode(non-dev).")
    try:
        host.reboot()
    except Exception as e:
        logging.debug(
                'Failed to reboot the DUT after switch to'
                ' non-dev mode; %s', e)
        try_reset_by_servo(host)
    logging.info("Install firmware completed successfully.")


def _start_firmware_update(host, result_file):
    """Run `chromeos-firmwareupdate` in background.

    In scenario servo v4 type C, some boards of DUT may lose ethernet
    connectivity on firmware update. There's no way to bring it back except
    rebooting the system.

    @param host         Host instance to use for servo and ssh operations.
    @param result_file  Path on DUT to save operation logs.

    @returns The process id."""
    # TODO(guocb): Use `make_dev_firmware` to re-sign from MP to test/dev.
    fw_update_cmd = 'chromeos-firmwareupdate --mode=factory --force'

    cmd = [
        "date > %s" % result_file,
        "nohup %s &>> %s" % (fw_update_cmd, result_file),
        "/usr/local/bin/hooks/check_ethernet.hook"
    ]
    return host.run_background(';'.join(cmd))


def _wait_firmware_update_process(host, pid, timeout=_FIRMWARE_UPDATE_TIMEOUT):
    """Wait `chromeos-firmwareupdate` to finish.

    @param host     Host instance to use for servo and ssh operations.
    @param pid      The process ID of `chromeos-firmwareupdate`.
    @param timeout  Maximum time to wait for firmware updating.
    """
    try:
        utils.poll_for_condition(
            lambda: host.run('ps -f -p %s' % pid, timeout=20).exit_status,
            exception=Exception(
                    "chromeos-firmwareupdate (pid: %s) didn't complete in %s "
                    'seconds.' % (pid, timeout)),
            timeout=_FIRMWARE_UPDATE_TIMEOUT,
            sleep_interval=10,
        )
    except error.AutoservRunError:
        # We lose the connectivity, so the DUT should be booting up.
        if not host.wait_up(timeout=host.USB_BOOT_TIMEOUT):
            raise Exception(
                    'DUT failed to boot up after firmware updating.')


def _check_firmware_update_result(host, result_file):
    """Check if firmware updating is good or not.

    @param host         Host instance to use for servo and ssh operations.
    @param result_file  Path of the file saving output of
                        `chromeos-firmwareupdate`.
    """
    fw_update_was_good = ">> DONE: Firmware updater exits successfully."
    result = host.run('cat %s' % result_file)
    if result.stdout.rstrip().rsplit('\n', 1)[1] != fw_update_was_good:
        raise Exception("chromeos-firmwareupdate failed!")


def _prepare_servo(servohost):
    """Prepare servo connected to host for installation steps.

    @param servohost  A server.hosts.servo_host.ServoHost object.
    """
    # Stopping `servod` on the servo host will force `repair()` to
    # restart it.  We want that restart for a few reasons:
    #   + `servod` caches knowledge about the image on the USB stick.
    #     We want to clear the cache to force the USB stick to be
    #     re-imaged unconditionally.
    #   + If there's a problem with servod that verify and repair
    #     can't find, this provides a UI through which `servod` can
    #     be restarted.
    servohost.run('stop servod PORT=%d' % servohost.servo_port,
                  ignore_status=True)
    servohost.repair()

    if not servohost.get_servo().probe_host_usb_dev():
        raise Exception('No USB stick detected on Servo host')


def setup_hwid_and_serialnumber(host):
    """Do initial setup for ChromeOS host.

    @param host    servers.host.Host object.
    """
    if not hasattr(host, 'host_info_store'):
        raise Exception('%s does not have host_info_store' % host.hostname)

    info = host.host_info_store.get()
    hwid = host.run('crossystem hwid', ignore_status=True).stdout
    serial_number = host.run('vpd -g serial_number', ignore_status=True).stdout

    if not hwid and not serial_number:
        raise Exception(
                'Failed to retrieve HWID and SerialNumber from host %s' %
                host.hostname)
    if not serial_number:
        raise Exception('Failed to retrieve SerialNumber from host %s' %
                        host.hostname)
    if not hwid:
        raise Exception('Failed to retrieve HWID from host %s' % host.hostname)

    info.attributes['HWID'] = hwid
    info.attributes['serial_number'] = serial_number
    if info != host.host_info_store.get():
        host.host_info_store.commit(info)
    logging.info("Reading HWID and SerialNumber completed successfully.")