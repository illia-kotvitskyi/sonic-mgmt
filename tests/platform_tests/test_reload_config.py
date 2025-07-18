"""
Check platform status after config is reloaded

This script is to cover the test case 'Reload configuration' in the SONiC platform test plan:
https://github.com/sonic-net/SONiC/blob/master/doc/pmon/sonic_platform_test_plan.md
"""
import logging
import time
import pytest

from tests.common.fixtures.conn_graph_facts import conn_graph_facts     # noqa: F401
from tests.common.utilities import wait_until
from tests.common.platform.processes_utils import wait_critical_processes
from tests.common.platform.transceiver_utils import check_transceiver_basic
from tests.common.platform.interface_utils import check_all_interface_information, get_port_map
from tests.common.reboot import reboot
from tests.common.config_reload import config_force_option_supported, config_system_checks_passed

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any')
]


@pytest.fixture(scope="module")
def delayed_services(duthosts, enum_rand_one_per_hwsku_hostname):
    """Return the delayed services."""
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    delayed_services = []

    # NOTE: in the follow versions, config reload checks for the delayed services
    # up states:
    # - 202205
    if any(version in duthost.os_version for version in ("202205",)):
        list_timer_out = duthost.shell(
            "systemctl list-dependencies --plain sonic-delayed.target | sed '1d'",
            module_ignore_errors=True
        )
        if not list_timer_out["failed"]:
            check_timer_out = duthost.shell(
                "systemctl is-enabled %s" % list_timer_out["stdout"].replace("\n", " "),
                module_ignore_errors=True
            )
            if not check_timer_out["failed"]:
                timers = [_.strip() for _ in list_timer_out["stdout"].strip().splitlines()]
                states = [_.strip() for _ in check_timer_out["stdout"].strip().splitlines()]
                delayed_services.extend(
                    timer.replace("timer", "service") for timer, state in zip(timers, states) if state == "enabled"
                )
    return delayed_services


def test_reload_configuration(duthosts, enum_rand_one_per_hwsku_hostname,
                              conn_graph_facts, xcvr_skip_list):       # noqa: F811
    """
    @summary: This test case is to reload the configuration and check platform status
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    interfaces = conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {})
    asic_type = duthost.facts["asic_type"]

    if config_force_option_supported(duthost):
        assert wait_until(360, 20, 0, config_system_checks_passed, duthost), (
            "System checks did not pass within the allotted time after config reload. "
            "Hostname: {}\n"
            "Platform: {}\n"
            "HWSKU: {}\n"
        ).format(
            duthost.hostname,
            duthost.facts.get("platform"),
            duthost.facts.get("hwsku")
        )

    logging.info("Reload configuration")
    duthost.shell("sudo config reload -y &>/dev/null", executable="/bin/bash")

    logging.info("Wait until all critical services are fully started")
    wait_critical_processes(duthost)

    logging.info("Wait some time for all the transceivers to be detected")
    max_wait_time_for_transceivers = 300
    if duthost.facts["platform"] in ["x86_64-cel_e1031-r0", "x86_64-88_lc0_36fh_m-r0"]:
        max_wait_time_for_transceivers = 900
    assert wait_until(max_wait_time_for_transceivers, 20, 0, check_all_interface_information,
                      duthost, interfaces, xcvr_skip_list), (
        "Not all transceivers detected in {timeout} seconds. "
        "- Hostname: {hostname}\n"
        "- Platform: {platform}\n"
        "- HWSKU: {hwsku}\n"
        "- Interfaces checked: {interfaces}\n"
        "- Max wait time (seconds): {timeout}"
    ).format(
        timeout=max_wait_time_for_transceivers,
        hostname=duthost.hostname,
        platform=duthost.facts.get('platform'),
        hwsku=duthost.facts.get('hwsku'),
        interfaces=list(interfaces.keys())
    )

    logging.info("Check transceiver status")
    for asic_index in duthost.get_frontend_asic_ids():
        # Get the interfaces pertaining to that asic
        interface_list = get_port_map(duthost, asic_index)
        interfaces_per_asic = {k: v for k, v in list(interface_list.items()) if k in interfaces}
        check_transceiver_basic(duthost, asic_index,
                                interfaces_per_asic, xcvr_skip_list)

    if asic_type in ["mellanox"]:

        from .mellanox.check_hw_mgmt_service import check_hw_management_service
        from .mellanox.check_sysfs import check_sysfs

        logging.info("Check the hw-management service")
        check_hw_management_service(duthost)

        logging.info("Check sysfs")
        check_sysfs(duthost)


def check_database_status(duthost):
    # check global database docker is running
    if not duthost.is_service_fully_started('database'):
        return False

    # For multi-asic check each asics database
    if duthost.is_multi_asic:
        for asic in duthost.asics:
            if not duthost.is_service_fully_started('database{}'.format(asic.asic_index)):
                return False

    return True


def execute_config_reload_cmd(duthost, timeout=120, check_interval=5):
    start_time = time.time()
    _, res = duthost.shell("sudo config reload -y",
                           executable="/bin/bash",
                           module_ignore_errors=True,
                           module_async=True)

    while not res.ready():
        elapsed_time = time.time() - start_time
        if elapsed_time > timeout:
            logging.info("Config reload command did not complete within {} seconds".format(timeout))
            return False, None

        logging.debug("Waiting for config reload command to complete. Elapsed time: {} seconds.".format(elapsed_time))
        time.sleep(check_interval)

    if res.successful():
        result = res.get()
        logging.debug("Config reload command result: {}".format(result))
        return True, result
    else:
        logging.info("Config reload command execution failed: {}".format(res))
        return False, None


def check_docker_status(duthost):
    containers = duthost.get_all_containers()
    for container in containers:
        if 'disabled' in duthost.get_feature_status()[0].get(container, ''):
            continue
        if not duthost.is_service_fully_started(container):
            return False
    return True


def test_reload_configuration_checks(duthosts, enum_rand_one_per_hwsku_hostname, delayed_services,
                                     localhost, conn_graph_facts, xcvr_skip_list):      # noqa: F811
    """
    @summary: This test case is to test various system checks in config reload
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    hwsku = duthost.facts["hwsku"]

    config_reload_timeout = 120
    if hwsku in ["Nokia-M0-7215", "Nokia-7215"]:
        config_reload_timeout = 180

    if not config_force_option_supported(duthost):
        return

    reboot(duthost, localhost, reboot_type="cold", return_after_reconnect=True)

    # Check if all database containers have started
    # Some device after reboot may take some longer time to have database container started up
    # we must give it a little longer or else it may falsely fail the test.
    wait_until(360, 1, 0, check_database_status, duthost)

    logging.info("Reload configuration check")

    # sometimes redis is not up in time (eg. t2 chassis), so retry until it's up
    for i in range(10):
        result, out = execute_config_reload_cmd(duthost, config_reload_timeout)

        # Redis is up - can stop looping
        if result and "RuntimeError: Unable to connect to redis" not in out['stderr']:
            break

        time.sleep(1)

    # config reload command shouldn't work immediately after system reboot
    assert result and "Retry later" in out['stdout'], (
        "The config reload command did not return the expected 'Retry later' message. "
        "Output: '{}'\n"
        "Hostname: {}\n"
        "Platform: {}\n"
        "HWSKU: {}\n"
    ).format(
        out['stdout'],
        duthost.hostname,
        duthost.facts.get("platform"),
        duthost.facts.get("hwsku")
    )

    assert wait_until(360, 20, 0, config_system_checks_passed, duthost, delayed_services), (
        "System checks did not pass within the allotted time after config reload on '{}'. Delayed services: {}"
    ).format(duthost.hostname, delayed_services)

    if not duthost.get_facts().get("modular_chassis"):
        # Check if all containers have started
        assert wait_until(300, 10, 0, check_docker_status, duthost), (
            "Not all Docker containers reached the 'fully started' state within 300 seconds "
            "after config reload on '{}'."
        ).format(duthost.hostname)

        # To ensure the system is stable enough, wait for another 30s
        time.sleep(30)

    # After the system checks succeed the config reload command should not throw error
    result, out = execute_config_reload_cmd(duthost, config_reload_timeout)
    assert result and "Retry later" not in out['stdout'], (
        "The config reload command returned an unexpected 'Retry later' message or failed to execute successfully. "
        "Output: '{}'\n"
        "Hostname: {}\n"
        "Platform: {}\n"
        "HWSKU: {}\n"
    ).format(
        out['stdout'],
        duthost.hostname,
        duthost.facts.get("platform"),
        duthost.facts.get("hwsku")
    )

    # Immediately after one config reload command, another shouldn't execute and wait for system checks
    logging.info("Checking config reload after system is up")
    # Check if all database containers have started
    wait_until(60, 1, 0, check_database_status, duthost)
    result, out = execute_config_reload_cmd(duthost, config_reload_timeout)
    assert result and "Retry later" in out['stdout'], (
        "The config reload command did not return the expected 'Retry later' message. "
        "Output: '{}'\n"
        "Hostname: {}\n"
        "Platform: {}\n"
        "HWSKU: {}\n"
    ).format(
        out['stdout'],
        duthost.hostname,
        duthost.facts.get("platform"),
        duthost.facts.get("hwsku")
    )

    assert wait_until(360, 20, 0, config_system_checks_passed, duthost, delayed_services), (
        "System checks did not pass within the allotted time after config reload on '{}'. Delayed services: {}"
    ).format(duthost.hostname, delayed_services)

    # Wait untill all critical processes come up so that it doesnt interfere with swss stop job
    wait_critical_processes(duthost)
    logging.info("Stopping swss docker and checking config reload")
    if duthost.is_multi_asic:
        for asic in duthost.asics:
            duthost.shell("sudo service swss@{} stop".format(asic.asic_index))
    else:
        duthost.shell("sudo service swss stop")

    # Without swss running config reload option should not proceed
    result, out = execute_config_reload_cmd(duthost, config_reload_timeout)
    assert result and "Retry later" in out['stdout'], (
        "The config reload command did not return the expected 'Retry later' message. "
        "Output: '{}'\n"
        "Hostname: {}\n"
        "Platform: {}\n"
        "HWSKU: {}\n"
    ).format(
        out['stdout'],
        duthost.hostname,
        duthost.facts.get("platform"),
        duthost.facts.get("hwsku")
    )

    # However with force option config reload should proceed
    logging.info("Performing force config reload")
    out = duthost.shell("sudo config reload -y -f", executable="/bin/bash")
    assert "Retry later" not in out['stdout'], (
        "The config reload command returned an unexpected 'Retry later' message or failed to execute successfully. "
        "Output: '{}'\n"
        "Hostname: {}\n"
        "Platform: {}\n"
        "HWSKU: {}\n"
    ).format(
        out['stdout'],
        duthost.hostname,
        duthost.facts.get("platform"),
        duthost.facts.get("hwsku")
    )

    assert wait_until(360, 20, 0, config_system_checks_passed, duthost, delayed_services), (
        "System checks did not pass within the allotted time after config reload on '{}'. Delayed services: {}"
    ).format(duthost.hostname, delayed_services)
