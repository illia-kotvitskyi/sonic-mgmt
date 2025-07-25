"""
        ptf --test-dir ptftests fast-reboot \
            --qlen=1000 \
            --platform remote \
            -t 'verbose=True;dut_username="admin";dut_hostname="10.0.0.243";reboot_limit_in_seconds=30;\
                portchannel_ports_file="/tmp/portchannel_interfaces.json";\
                vlan_ports_file="/tmp/vlan_interfaces.json";ports_file="/tmp/ports.json";\
                peer_ports_file="/tmp/peer_ports.json";dut_mac="4c:76:25:f5:48:80";\
                default_ip_range="192.168.0.0/16";vlan_ip_range="{\"Vlan100\": \"172.0.0.0/22\"}";\
                arista_vms="[\"10.0.0.200\",\"10.0.0.201\",\"10.0.0.202\",\"10.0.0.203\"]"' \
            --platform-dir ptftests \
            --disable-vxlan \
            --disable-geneve \
            --disable-erspan \
            --disable-mpls \
            --disable-nvgre

"""
#
# This test checks that DUT is able to make FastReboot procedure
#
# This test supposes that fast-reboot/warm-reboot initiates by running /usr/bin/{fast,warm}-reboot command.
#
# The test uses "pings". The "pings" are packets which are sent through dataplane in two directions
# 1. From one of vlan interfaces to T1 device. The source ip, source interface,
#    and destination IP are chosen randomly from valid choices. Number of packet is 100.
# 2. From all of portchannel ports to all of vlan ports. The source ip, source interface,
#    and destination IP are chosed sequentially from valid choices.
#    Currently we have 500 distrinct destination vlan addresses. Our target to have 1000 of them.
#
# The test sequence is following:
# 1. Check that DUT is stable. That means that "pings" work in both directions:
#    from T1 to servers and from servers to T1.
# 2. If DUT is stable the test starts continiously pinging DUT in both directions.
# 3. The test runs '/usr/bin/{fast,warm}-reboot' on DUT remotely.
#    The ssh key supposed to be uploaded by ansible before the test
# 4. As soon as it sees that ping starts failuring in one of directions
#    the test registers a start of dataplace disruption
# 5. As soon as the test sees that pings start working for DUT in both directions
#    it registers a stop of dataplane disruption
# 6. If the length of the disruption is less than 30 seconds (if not redefined by parameter) - the test passes
# 7. If there're any drops, when control plane is down - the test fails
# 8. When test start reboot procedure it connects to all VM (which emulates T1)
#    and starts fetching status of BGP and LACP
#    LACP is supposed to be down for one time only, if not - the test fails
#    if default value of BGP graceful restart timeout is less than 120 seconds the test fails
#    if BGP graceful restart is not enabled on DUT the test fails
#    If BGP graceful restart timeout value is almost exceeded (less than 15 seconds) the test fails
#    if BGP routes disappeares more then once, the test failed
#
# The test expects you're running the test with link state propagation helper.
# That helper propagate a link state from fanout switch port to corresponding VM port
#

import os
import random
import struct
import datetime
import time
import json
import subprocess
import threading
import traceback
import multiprocessing
import itertools
import ast
import socket

import ptf
import ptf.testutils as testutils
import ptf.packet as scapy
import scapy.all as scapyall
from scapy.arch.linux import attach_filter as attach_filter

import sad_path as sp

from ptf import config
from ptf.base_tests import BaseTest
from ptf.testutils import simple_tcp_packet, simple_icmp_packet, simple_arp_packet
from ptf.mask import Mask

from six.moves import _thread as thread
from six.moves import queue as Queue
from multiprocessing.pool import ThreadPool, TimeoutError
from fcntl import ioctl
from collections import defaultdict
from device_connection import DeviceConnection
from host_device import HostDevice


class StateMachine():
    def __init__(self, init_state='init'):
        self.state_lock = threading.RLock()
        self.state_time = {}  # Recording last time when entering a state
        self.state = None
        self.flooding = False
        self.set(init_state)

    def set(self, state):
        with self.state_lock:
            self.state = state
            self.state_time[state] = datetime.datetime.now()

    def get(self):
        with self.state_lock:
            cur_state = self.state
        return cur_state

    def get_state_time(self, state):
        with self.state_lock:
            time = self.state_time[state]
        return time

    def set_flooding(self, flooding):
        with self.state_lock:
            self.flooding = flooding

    def is_flooding(self):
        with self.state_lock:
            flooding = self.flooding

        return flooding


class ReloadTest(BaseTest):
    TIMEOUT = 0.5
    PKT_TOUT = 1
    VLAN_BASE_MAC_PATTERN = '72060001{:04}'
    LAG_BASE_MAC_PATTERN = '5c010203{:04}'
    SOCKET_RECV_BUFFER_SIZE = 10 * 1024 * 1024

    def __init__(self):
        BaseTest.__init__(self)
        self.fails = {}
        self.info = {}
        self.cli_info = {}
        self.logs_info = {}
        self.lacp_pdu_times = {}
        self.log_lock = threading.RLock()
        self.vm_handle = None
        self.sad_handle = None
        self.process_id = str(os.getpid())
        self.test_params = testutils.test_params_get()
        self.check_param('verbose', False, required=False)
        self.check_param('dut_username', '', required=True)
        self.check_param('dut_password', '', required=True)
        self.check_param('dut_hostname', '', required=True)
        self.check_param('reboot_limit_in_seconds', 30, required=False)
        self.check_param('reboot_type', 'fast-reboot', required=False)
        self.check_param('graceful_limit', 240, required=False)
        self.check_param('portchannel_ports_file', '', required=True)
        self.check_param('vlan_ports_file', '', required=True)
        self.check_param('ports_file', '', required=True)
        self.check_param('peer_ports_file', '', required=False)
        self.check_param('dut_mux_status', '', required=False)
        self.check_param('dut_mac', '', required=True)
        self.check_param('vlan_mac', '', required=True)
        self.check_param('default_ip_range', '', required=True)
        self.check_param('vlan_ip_range', '', required=True)
        self.check_param('lo_prefix', '', required=False)
        self.check_param('lo_v6_prefix', 'fc00:1::/64', required=False)
        self.check_param('arista_vms', [], required=True)
        self.check_param('min_bgp_gr_timeout', 15, required=False)
        self.check_param('warm_up_timeout_secs', 300, required=False)
        self.check_param('dut_stabilize_secs', 30, required=False)
        self.check_param('preboot_files', None, required=False)
        # preboot sad path to inject before warm-reboot
        self.check_param('preboot_oper', None, required=False)
        # sad path to inject during warm-reboot
        self.check_param('inboot_oper', None, required=False)
        # nexthops for the routes that will be added during warm-reboot
        self.check_param('nexthop_ips', [], required=False)
        self.check_param('allow_vlan_flooding', False, required=False)
        self.check_param('allow_mac_jumping', False, required=False)
        self.check_param('sniff_time_incr', 300, required=False)
        self.check_param('vnet', False, required=False)
        self.check_param('vnet_pkts', None, required=False)
        self.check_param('target_version', '', required=False)
        self.check_param('bgp_v4_v6_time_diff', 40, required=False)
        self.check_param('asic_type', '', required=False)
        self.check_param('logfile_suffix', None, required=False)
        self.check_param('neighbor_type', 'eos', required=False)
        self.check_param('ceos_neighbor_lacp_multiplier', 3, required=False)
        self.check_param('port_channel_intf_idx', [], required=False)
        if not self.test_params['preboot_oper'] or self.test_params['preboot_oper'] == 'None':
            self.test_params['preboot_oper'] = None
        if not self.test_params['inboot_oper'] or self.test_params['inboot_oper'] == 'None':
            self.test_params['inboot_oper'] = None

        self.dataplane_loss_checked_successfully = False

        # initialize sad oper
        if self.test_params['preboot_oper']:
            self.sad_oper = self.test_params['preboot_oper']
        else:
            self.sad_oper = self.test_params['inboot_oper']

        if self.test_params['logfile_suffix']:
            self.logfile_suffix = self.test_params['logfile_suffix']
        else:
            self.logfile_suffix = self.sad_oper

        if "warm-reboot" in self.test_params['reboot_type']:
            reboot_log_prefix = "warm-reboot"
        else:
            reboot_log_prefix = self.test_params['reboot_type']
        if self.logfile_suffix:
            self.log_file_name = '/tmp/%s-%s.log' % (
                reboot_log_prefix, self.logfile_suffix)
            self.report_file_name = '/tmp/%s-%s-report.json' % (
                reboot_log_prefix, self.logfile_suffix)
        else:
            self.log_file_name = '/tmp/%s.log' % reboot_log_prefix
            self.report_file_name = '/tmp/%s-report.json' % reboot_log_prefix
        self.report = dict()
        self.log_fp = open(self.log_file_name, 'w')

        self.packets_list = []
        self.vnet = self.test_params['vnet']
        if (self.vnet):
            self.packets_list = json.load(open(self.test_params['vnet_pkts']))

        # a flag whether to populate FDB by sending traffic from simulated servers
        # usually ARP responder will make switch populate its FDB table, but Mellanox on 201803 has
        # no L3 ARP support, so this flag is used to W/A this issue
        self.setup_fdb_before_test = self.test_params.get(
            'setup_fdb_before_test', False)

        # Default settings
        self.ping_dut_pkts = 10
        self.arp_ping_pkts = 1
        self.arp_vlan_gw_ping_pkts = 10
        self.nr_pc_pkts = 100
        self.nr_tests = 3
        self.reboot_delay = 10
        self.control_plane_down_timeout = 600   # Wait up to 6 minutes for control plane down
        self.task_timeout = 300   # Wait up to 5 minutes for tasks to complete
        self.max_nr_vl_pkts = 500  # FIXME: should be 1000.
        # But ptf is not fast enough + swss is slow for FDB and ARP entries insertions
        self.timeout_thr = None

        # Listen for more then 240 seconds, to be used in sniff_in_background method.
        self.time_to_listen = 240.0
        #   Inter-packet interval, to be used in send_in_background method.
        #   Improve this interval to gain more precision of disruptions.
        self.send_interval = 0.0035
        self.sent_packet_count = 0
        # Thread pool for background watching operations
        self.pool = ThreadPool(processes=3)

        # State watcher attributes
        self.watching = False
        self.cpu_state = StateMachine('init')
        self.asic_state = StateMachine('init')
        self.vlan_state = StateMachine('init')
        self.vlan_gw_state = StateMachine('init')
        self.vlan_lock = threading.RLock()
        self.asic_state_time = {}  # Recording last asic state entering time
        self.asic_vlan_reach = []  # Recording asic vlan reachability
        self.recording = False  # Knob for recording asic_vlan_reach
        self.finalizer_state = ''
        # light_probe:
        #    True : when one direction probe fails, don't probe another.
        #    False: when one direction probe fails, continue probe another.
        self.light_probe = False
        # We have two data plane traffic generators which are mutualy exclusive
        # one is the reachability_watcher thread
        # second is the fast send_in_background
        self.dataplane_io_lock = threading.Lock()

        self.allow_vlan_flooding = bool(
            self.test_params['allow_vlan_flooding'])

        self.dut_connection = DeviceConnection(
            self.test_params['dut_hostname'],
            self.test_params['dut_username'],
            password=self.test_params['dut_password'],
            alt_password=self.test_params.get('alt_password')
        )
        self.installed_sonic_version = self.get_installed_sonic_version()
        self.sender_thr = threading.Thread(target=self.send_in_background)
        self.sniff_thr = threading.Thread(target=self.sniff_in_background)

        # Check if platform type is kvm
        stdout, stderr, return_code = self.dut_connection.execCommand(
            "show platform summary | grep Platform | awk '{print $2}'")
        platform_type = str(stdout[0]).replace('\n', '')
        if platform_type == 'x86_64-kvm_x86_64-r0':
            self.kvm_test = True
        else:
            self.kvm_test = False
        if "service-warm-restart" in self.test_params['reboot_type']:
            self.check_param('service_list', None, required=True)
            self.check_param('service_data', None, required=True)
            self.service_data = self.test_params['service_data']
            for service_name in self.test_params['service_list']:
                cmd = 'systemctl show -p ExecMainStartTimestamp {}'.format(
                    service_name)
                stdout, _, _ = self.dut_connection.execCommand(cmd)
                if service_name not in self.service_data:
                    self.service_data[service_name] = {}
                self.service_data[service_name]['service_start_time'] = str(
                    stdout[0]).strip()
                self.log("Service start time for {} is {}".format(
                    service_name, self.service_data[service_name]['service_start_time']))
        return

    def read_json(self, name):
        with open(self.test_params[name]) as fp:
            content = json.load(fp)

        return content

    def read_port_indices(self):
        port_indices = self.read_json('ports_file')
        peer_port_indices = {}
        if self.is_dualtor:
            peer_port_indices = self.read_json('peer_ports_file')

        return port_indices, peer_port_indices

    def read_mux_status(self):
        active_port_indices = []
        mux_status = self.read_json('dut_mux_status')
        for intf, port in self.port_indices.items():
            if intf in mux_status and mux_status[intf]['status'] == 'active':
                active_port_indices.append(port)

        return active_port_indices

    def read_vlan_portchannel_ports(self):
        portchannel_content = self.read_json('portchannel_ports_file')
        portchannel_names = [pc['name'] for pc in portchannel_content.values()]

        vlan_content = self.read_json('vlan_ports_file')

        ports_per_vlan = dict()
        pc_in_vlan = []
        for vlan in self.vlan_ip_range.keys():
            ports_in_vlan = []
            for ifname in vlan_content[vlan]['members']:
                if ifname in portchannel_names:
                    pc_in_vlan.append(ifname)
                else:
                    ports_in_vlan.append(self.port_indices[ifname])
            ports_per_vlan[vlan] = ports_in_vlan

        active_portchannels = list()
        for neighbor_info in list(self.vm_dut_map.values()):
            active_portchannels.append(neighbor_info["dut_portchannel"])

        pc_ifaces = []
        for pc in portchannel_content.values():
            if not pc['name'] in pc_in_vlan and pc['name'] in active_portchannels:
                pc_ifaces.extend([self.port_indices[member]
                                 for member in pc['members']])

        dualtor_pc_ifaces = []
        if self.is_dualtor:
            peer_active_portchannels = list()
            for neighbor_info in list(self.peer_vm_dut_map.values()):
                peer_active_portchannels.append(neighbor_info["dut_portchannel"])
            dualtor_pc_ifaces.extend(pc_ifaces)
            for pc in portchannel_content.values():
                if not pc['name'] in pc_in_vlan and pc['name'] in peer_active_portchannels:
                    dualtor_pc_ifaces.extend([self.peer_port_indices[member] for member in pc['members']])

        return ports_per_vlan, pc_ifaces, dualtor_pc_ifaces

    def check_param(self, param, default, required=False):
        if param not in self.test_params:
            if required:
                raise Exception("Test parameter '%s' is required" % param)
            self.test_params[param] = default

    def random_ip(self, ip):
        net_addr, mask = ip.split('/')
        n_hosts = 2**(32 - int(mask))
        random_host = random.randint(2, n_hosts - 2)
        return self.host_ip(ip, random_host)

    def host_ip(self, net_ip, host_number):
        src_addr, mask = net_ip.split('/')
        n_hosts = 2**(32 - int(mask))
        if host_number > (n_hosts - 2):
            raise Exception("host number %d is greater than number of hosts %d in the network %s" % (
                host_number, n_hosts - 2, net_ip))
        src_addr_n = struct.unpack(">I", socket.inet_aton(src_addr))[0]
        net_addr_n = src_addr_n & (2**32 - n_hosts)
        host_addr_n = net_addr_n + host_number
        host_ip = socket.inet_ntoa(struct.pack(">I", host_addr_n))

        return host_ip

    def random_port(self, ports):
        return random.choice(ports)

    def log(self, message, verbose=False):
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_lock:
            if verbose and self.test_params['verbose'] or not verbose:
                print("%s : %s" % (current_time, message))
            self.log_fp.write("%s : %s\n" % (current_time, message))
            self.log_fp.flush()

    def timeout(self, func, seconds, message):
        signal = multiprocessing.Event()
        async_res = self.pool.apply_async(func, args=(signal,))

        try:
            res = async_res.get(timeout=seconds)
        except Exception as err:
            traceback_msg = traceback.format_exc()
            # TimeoutError and Exception's from func
            # captured here
            signal.set()
            self.log("{}: {}".format(message, traceback_msg))
            raise type(err)("{}: {}".format(message, traceback_msg))
        return res

    def generate_vlan_servers(self):
        vlan_host_map = defaultdict(dict)
        self.vlan_host_ping_map = defaultdict(dict)
        self.nr_vl_pkts = 0     # Number of packets from upper layer
        for vlan, prefix in self.vlan_ip_range.items():
            if not self.ports_per_vlan[vlan]:
                continue
            _, mask = prefix.split('/')
            n_hosts = min(2**(32 - int(mask)) - 3, self.max_nr_vl_pkts)

            for counter, i in enumerate(range(2, n_hosts + 2)):
                mac = self.VLAN_BASE_MAC_PATTERN.format(counter)
                port = self.ports_per_vlan[vlan][i %
                                                 len(self.ports_per_vlan[vlan])]
                addr = self.host_ip(prefix, i)

                vlan_host_map[port][addr] = mac

            for counter, i in enumerate(
                    range(n_hosts+2, n_hosts+2+len(self.ports_per_vlan[vlan])), start=n_hosts):
                mac = self.VLAN_BASE_MAC_PATTERN.format(counter)
                port = self.ports_per_vlan[vlan][i %
                                                 len(self.ports_per_vlan[vlan])]
                try:
                    addr = self.host_ip(prefix, i)
                except Exception as e:
                    # If the number of hosts exceeds the number of available IPs in the subnet
                    # half host number to avoid the exception and ip collision
                    self.log("Capture exception for host_ip: {}".format(repr(e)))
                    addr = self.host_ip(prefix, int(i//2))
                self.vlan_host_ping_map[port][addr] = mac

            self.nr_vl_pkts += n_hosts

        return vlan_host_map

    def generate_arp_responder_conf(self, vlan_host_map):
        arp_responder_conf = {}
        for port in vlan_host_map:
            arp_responder_conf['eth{}'.format(port)] = {}
            arp_responder_conf['eth{}'.format(
                port)].update(vlan_host_map[port])
            arp_responder_conf['eth{}'.format(port)].update(
                self.vlan_host_ping_map[port])

        return arp_responder_conf

    def dump_arp_responder_config(self, dump):
        # save data for arp_replay process
        filename = "/tmp/from_t1.json" if self.logfile_suffix is None else "/tmp/from_t1_%s.json" % self.logfile_suffix
        with open(filename, "w") as fp:
            json.dump(dump, fp)

    def get_peer_dev_info(self):
        content = self.read_json('peer_dev_info')
        for key in content.keys():
            if 'ARISTA' in key:
                self.vm_dut_map[key] = dict()
                self.vm_dut_map[key]['mgmt_addr'] = content[key]['mgmt_addr']
                # initialize all the port mapping
                self.vm_dut_map[key]['dut_ports'] = []
                self.vm_dut_map[key]['neigh_ports'] = []
                self.vm_dut_map[key]['ptf_ports'] = []
                if self.is_dualtor:
                    self.peer_vm_dut_map[key] = dict()
                    self.peer_vm_dut_map[key]['dut_ports'] = []

    def get_portchannel_info(self):
        content = self.read_json('portchannel_ports_file')
        for key in content.keys():
            for member in content[key]['members']:
                for vm_key in self.vm_dut_map.keys():
                    if member in self.vm_dut_map[vm_key]['dut_ports']:
                        self.vm_dut_map[vm_key]['dut_portchannel'] = str(key)
                        neigh_portchannel = "PortChannel1" if self.test_params['neighbor_type'] == "sonic" \
                                            else "Port-Channel1"
                        self.vm_dut_map[vm_key]['neigh_portchannel'] = neigh_portchannel
                        if self.is_dualtor:
                            self.peer_vm_dut_map[vm_key]['dut_portchannel'] = str(key)
                        break

    def get_neigh_port_info(self):
        content = self.read_json('neigh_port_info')
        for key in content.keys():
            if content[key]['name'] in self.vm_dut_map.keys():
                self.vm_dut_map[content[key]['name']]['dut_ports'].append(str(key))
                self.vm_dut_map[content[key]['name']]['neigh_ports'].append(str(content[key]['port']))
                self.vm_dut_map[content[key]['name']]['ptf_ports'].append(self.port_indices[key])
                if self.is_dualtor:
                    self.peer_vm_dut_map[content[key]['name']]['dut_ports'].append(str(key))

    def build_peer_mapping(self):
        '''
            Builds a map of the form
                    'ARISTA01T1': {'mgmt_addr':
                                   'neigh_portchannel'
                                   'dut_portchannel'
                                   'neigh_ports'
                                   'dut_ports'
                                   'ptf_ports'
                                    }
        '''
        self.vm_dut_map = {}
        self.peer_vm_dut_map = {}
        for file in self.test_params['preboot_files'].split(','):
            self.test_params[file] = '/tmp/' + file + '.json'
        self.get_peer_dev_info()
        self.get_neigh_port_info()
        self.get_portchannel_info()

    def build_vlan_if_port_mapping(self):
        portchannel_content = self.read_json('portchannel_ports_file')
        portchannel_names = [pc['name'] for pc in portchannel_content.values()]

        vlan_content = self.read_json('vlan_ports_file')

        vlan_if_port = []
        for vlan in self.vlan_ip_range:
            for ifname in vlan_content[vlan]['members']:
                if ifname not in portchannel_names:
                    vlan_if_port.append((ifname, self.port_indices[ifname]))
        return vlan_if_port

    def populate_fail_info(self, fails):
        for key in fails:
            if key not in self.fails:
                self.fails[key] = set()
            self.fails[key] |= fails[key]

    def get_sad_info(self):
        '''
        Prepares the msg string to log when a sad_oper is defined. Sad oper can be a preboot or inboot oper
        sad_oper can be represented in the following ways
           eg. 'preboot_oper' - a single VM will be selected and preboot_oper will be applied to it
               'neigh_bgp_down:2' - 2 VMs will be selected and preboot_oper will be applied to the selected 2 VMs
               'neigh_lag_member_down:3:1' - this case is used for lag member down operation only.
                   This indicates that 3 VMs will be selected and 1 of
                   the lag members in the porchannel will be brought down
               'inboot_oper' - represents a routing change during warm boot (add or del of multiple routes)
               'routing_add:10' - adding 10 routes during warm boot
        '''
        msg = ''
        if self.sad_oper:
            msg = 'Sad oper: %s ' % self.sad_oper
            if ':' in self.sad_oper:
                oper_list = self.sad_oper.split(':')
                # extract the sad oper_type
                msg = 'Sad oper: %s ' % oper_list[0]
                if len(oper_list) > 2:
                    # extract the number of VMs and the number of LAG members.
                    # sad_oper will be of the form oper:no of VMS:no of lag members
                    msg += 'Number of sad path VMs: %s Lag member down in a portchannel: %s' % (
                        oper_list[-2], oper_list[-1])
                else:
                    # inboot oper
                    if 'routing' in self.sad_oper:
                        msg += 'Number of ip addresses: %s' % oper_list[-1]
                    else:
                        # extract the number of VMs. preboot_oper will be of the form oper:no of VMS
                        msg += 'Number of sad path VMs: %s' % oper_list[-1]

        return msg

    def init_sad_oper(self):
        if self.sad_oper:
            self.log("Preboot/Inboot Operations:")
            self.sad_handle = sp.SadTest(self.sad_oper, self.ssh_targets, self.portchannel_ports,
                                         self.vm_dut_map, self.test_params, self.vlan_ports, self.ports_per_vlan)
            (self.ssh_targets, self.portchannel_ports, self.neigh_vm, self.vlan_ports,
             self.ports_per_vlan), (log_info, fails) = self.sad_handle.setup()
            self.populate_fail_info(fails)
            for log in log_info:
                self.log(log)

            if self.sad_oper:
                log_info, fails = self.sad_handle.verify()
                self.populate_fail_info(fails)
                for log in log_info:
                    self.log(log)
                self.log(" ")

    def do_inboot_oper(self):
        '''
        Add or del routes during boot
        '''
        if self.sad_oper and 'routing' in self.sad_oper:
            self.log("Performing inboot operation")
            log_info, fails = self.sad_handle.route_setup()
            self.populate_fail_info(fails)
            for log in log_info:
                self.log(log)
            self.log(" ")

    def check_inboot_sad_status(self):
        if 'routing_add' in self.sad_oper:
            self.log('Verify if new routes added during warm reboot are received')
        else:
            self.log('Verify that routes deleted during warm reboot are removed')

        log_info, fails = self.sad_handle.verify(pre_check=False, inboot=True)
        self.populate_fail_info(fails)
        for log in log_info:
            self.log(log)
        self.log(" ")

    def check_postboot_sad_status(self):
        self.log("Postboot checks:")
        log_info, fails = self.sad_handle.verify(pre_check=False, inboot=False)
        self.populate_fail_info(fails)
        for log in log_info:
            self.log(log)
        self.log(" ")

    def sad_revert(self):
        self.log("Revert to preboot state:")
        log_info, fails = self.sad_handle.revert()
        self.populate_fail_info(fails)
        for log in log_info:
            self.log(log)
        self.log(" ")

    def setUp(self):
        self.fails['dut'] = set()
        self.fails['infrastructure'] = set()
        self.dut_mac = self.test_params['dut_mac']
        self.vlan_mac = self.test_params['vlan_mac']
        self.lo_prefix = self.test_params['lo_prefix']
        if self.vlan_mac != self.dut_mac:
            self.is_dualtor = True
        else:
            self.is_dualtor = False
        self.port_indices, self.peer_port_indices = self.read_port_indices()
        if self.is_dualtor:
            self.active_port_indices = self.read_mux_status()
        self.vlan_ip_range = ast.literal_eval(self.test_params['vlan_ip_range'])
        self.build_peer_mapping()
        self.ports_per_vlan, self.portchannel_ports, self.dualtor_portchannel_ports = \
            self.read_vlan_portchannel_ports()
        self.vlan_ports = []
        for ports in self.ports_per_vlan.values():
            self.vlan_ports += ports
        if self.sad_oper:
            self.test_params['vlan_if_port'] = self.build_vlan_if_port_mapping()

        self.default_ip_range = self.test_params['default_ip_range']

        self.limit = datetime.timedelta(
            seconds=self.test_params['reboot_limit_in_seconds'])
        self.reboot_type = self.test_params['reboot_type']
        if self.reboot_type in ['soft-reboot', 'reboot']:
            raise ValueError('Not supported reboot_type %s' % self.reboot_type)

        if self.kvm_test:
            self.log("This test is for KVM platform")

        # get VM info
        if isinstance(self.test_params['arista_vms'], list):
            arista_vms = self.test_params['arista_vms']
        else:
            arista_vms = self.test_params['arista_vms'][1:-1].split(",")
        self.ssh_targets = []
        for vm in arista_vms:
            if (vm.startswith("'") or vm.startswith('"')) and (vm.endswith("'") or vm.endswith('"')):
                self.ssh_targets.append(vm[1:-1])
            else:
                self.ssh_targets.append(vm)

        self.log("Converted addresses VMs: %s" % str(self.ssh_targets))
        self.init_sad_oper()

        self.vlan_host_map = self.generate_vlan_servers()
        arp_responder_conf = self.generate_arp_responder_conf(
            self.vlan_host_map)
        self.dump_arp_responder_config(arp_responder_conf)

        self.random_vlan = random.choice(self.vlan_ports)
        self.from_server_src_port = self.random_vlan
        self.from_server_src_addr = random.choice(list(self.vlan_host_map[self.random_vlan].keys()))
        self.from_server_src_mac = self.hex_to_mac(self.vlan_host_map[self.random_vlan][self.from_server_src_addr])
        self.from_server_dst_addr = self.random_ip(self.test_params['default_ip_range'])
        self.from_server_dst_ports = self.dualtor_portchannel_ports if self.is_dualtor else self.portchannel_ports

        self.log("Test params:")
        self.log("DUT ssh: %s@%s" %
                 (self.test_params['dut_username'], self.test_params['dut_hostname']))
        self.log("DUT reboot limit in seconds: %s" % self.limit)
        self.log("DUT mac address: %s" % self.dut_mac)
        self.log("DUT vlan mac address: %s" % self.vlan_mac)

        self.log("From server src addr: %s" % self.from_server_src_addr)
        self.log("From server src port: %s" % self.from_server_src_port)
        self.log("From server dst addr: %s" % self.from_server_dst_addr)
        self.log("From server dst ports: %s" % self.from_server_dst_ports)
        self.log("From upper layer number of packets: %d" % self.nr_vl_pkts)
        self.log("VMs: %s" % str(self.test_params['arista_vms']))

        self.log("Reboot type is %s" % self.reboot_type)

        self.generate_from_t1()
        self.generate_from_vlan()
        self.generate_ping_dut_lo()
        self.generate_arp_ping_packet()
        self.generate_arp_vlan_gw_packets()

        if 'warm-reboot' in self.reboot_type:
            self.log(self.get_sad_info())

        self.dataplane = ptf.dataplane_instance
        for p in self.dataplane.ports.values():
            port = p.get_packet_source()
            port.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, self.SOCKET_RECV_BUFFER_SIZE)

        self.dataplane.flush()
        if config["log_dir"] is not None:
            filename = os.path.join(config["log_dir"], str(self)) + ".pcap"
            self.dataplane.start_pcap(filename)

        self.log("Enabling arp_responder")
        self.cmd(["supervisorctl", "restart", "arp_responder"])

        # Give arp_responder 15 seconds to start up, because with the libpcap backend, scapy will first get information
        # about all of the interfaces on the system (which takes a bit of time) and then proceeds.
        self.log("Waiting 15 seconds for ARP responder to complete initialization")
        time.sleep(15)

        return

    def setup_fdb(self):
        """ simulate traffic generated from servers to help populate FDB """

        vlan_map = self.vlan_host_map

        from_servers_pkt = testutils.simple_tcp_packet(
            eth_dst=self.dut_mac,
            ip_dst=self.from_server_dst_addr,
        )

        for port in vlan_map:
            for addr in vlan_map[port]:
                mac = vlan_map[port][addr]

                from_servers_pkt[scapy.Ether].src = self.hex_to_mac(mac)
                from_servers_pkt[scapy.IP].src = addr

                testutils.send(self, port, from_servers_pkt)

        # make sure orchagent processed new FDBs
        time.sleep(1)

    def tearDown(self):

        self.log("Disabling arp_responder")
        self.cmd(["supervisorctl", "stop", "arp_responder"])

        # Stop watching DUT
        self.watching = False

        if config["log_dir"] is not None:
            self.dataplane.stop_pcap()
        self.log_fp.close()

    def get_if(self, iff, cmd):
        s = socket.socket()
        ifreq = ioctl(s, cmd, struct.pack("16s16x", iff))
        s.close()

        return ifreq

    @staticmethod
    def hex_to_mac(hex_mac):
        return ':'.join(hex_mac[i:i+2] for i in range(0, len(hex_mac), 2))

    def generate_from_t1(self):
        self.from_t1 = []

        # for each server host create a packet destinating server IP
        for counter, host_port in enumerate(self.vlan_host_map):
            src_addr = self.random_ip(self.default_ip_range)
            src_port = self.random_port(self.portchannel_ports)

            for server_ip in self.vlan_host_map[host_port]:
                dst_addr = server_ip

                # generate source MAC address for traffic based on LAG_BASE_MAC_PATTERN
                mac_addr = self.hex_to_mac(
                    self.LAG_BASE_MAC_PATTERN.format(counter))

                packet = simple_tcp_packet(eth_src=mac_addr,
                                           eth_dst=self.dut_mac,
                                           ip_src=src_addr,
                                           ip_dst=dst_addr,
                                           ip_ttl=255,
                                           tcp_dport=5000)

                self.from_t1.append((src_port, bytes(packet)))

        # expect any packet with dport 5000
        exp_packet = simple_tcp_packet(
            ip_src="0.0.0.0",
            ip_dst="0.0.0.0",
            tcp_dport=5000,
        )

        self.from_t1_exp_packet = Mask(exp_packet)
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.Ether, "src")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.Ether, "dst")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "src")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "dst")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "chksum")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.TCP, "chksum")
        self.from_t1_exp_packet.set_do_not_care_scapy(scapy.IP, "ttl")

    def generate_from_vlan(self):
        self.from_servers = []
        for _, from_port in enumerate(self.vlan_host_map):
            for server_ip in self.vlan_host_map[from_port]:
                from_server_src_addr = server_ip
                from_server_src_mac = self.hex_to_mac(self.vlan_host_map[from_port][from_server_src_addr])

                packet = simple_tcp_packet(
                    eth_src=from_server_src_mac,
                    eth_dst=self.vlan_mac,
                    ip_src=from_server_src_addr,
                    ip_dst=self.from_server_dst_addr,
                    tcp_dport=5000
                )

                self.from_servers.append((from_port, bytes(packet)))

        exp_packet = simple_tcp_packet(
            ip_dst=self.from_server_dst_addr,
            ip_ttl=63,
            tcp_dport=5000,
        )

        self.from_vlan_exp_packet = Mask(exp_packet)

        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.IP, "src")
        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.IP, "chksum")
        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.TCP, "chksum")
        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.IP, "id")
        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.Ether, "src")
        self.from_vlan_exp_packet.set_do_not_care_scapy(scapy.Ether, "dst")

        self.watcher_from_server_iter = itertools.cycle(self.from_servers)
        self.log("Prepared {} packets from servers".format(len(self.from_servers)))

    def generate_ping_dut_lo(self):
        self.ping_dut_packets = []
        dut_lo_ipv4 = self.lo_prefix.split('/')[0]

        for src_port in self.active_port_indices if self.is_dualtor else self.vlan_host_ping_map:
            src_addr = random.choice(list(self.vlan_host_ping_map[src_port].keys()))
            src_mac = self.hex_to_mac(
                self.vlan_host_ping_map[src_port][src_addr])
            packet = simple_icmp_packet(eth_src=src_mac,
                                        eth_dst=self.vlan_mac,
                                        ip_src=src_addr,
                                        ip_dst=dut_lo_ipv4)
            self.ping_dut_packets.append((src_port, bytes(packet)))

        exp_packet = simple_icmp_packet(eth_src=self.vlan_mac,
                                        ip_src=dut_lo_ipv4,
                                        icmp_type='echo-reply')

        self.ping_dut_macjump_packet = simple_icmp_packet(eth_dst=self.dut_mac,
                                                          ip_src=self.from_server_src_addr,
                                                          ip_dst=dut_lo_ipv4)

        self.ping_dut_exp_packet = Mask(exp_packet)
        self.ping_dut_exp_packet.set_do_not_care_scapy(scapy.Ether, "dst")
        self.ping_dut_exp_packet.set_do_not_care_scapy(scapy.IP, "dst")
        self.ping_dut_exp_packet.set_do_not_care_scapy(scapy.IP, "id")
        self.ping_dut_exp_packet.set_do_not_care_scapy(scapy.IP, "chksum")

    def calc_offset_and_size(self, packet, layer, field):
        """
        Calculate the offset and size of a field, in a packet. Return the offset and size
        as a tuple, both in bits. Return -1, 0 if the field cannot be found.
        """
        offset = 0
        while packet:  # for each payload
            for fld in packet.fields_desc:  # for each field
                if fld.name == field and isinstance(packet, layer):
                    return int(offset) * 8, fld.i2len(packet, packet.getfieldval(fld.name)) * 8
                offset += fld.i2len(packet, packet.getfieldval(fld.name))  # add length
            packet = packet.payload
        return -1, 0

    def generate_arp_ping_packet(self):
        vlan = next(k for k, v in self.ports_per_vlan.items() if v)
        vlan_ip_range = self.vlan_ip_range[vlan]

        vlan_port_canadiates = list(range(len(self.ports_per_vlan[vlan])))
        vlan_port_canadiates.remove(0)  # subnet prefix
        vlan_port_canadiates.remove(1)  # subnet IP on dut
        src_idx = random.choice(vlan_port_canadiates)
        vlan_port_canadiates.remove(src_idx)
        dst_idx = random.choice(vlan_port_canadiates)
        src_port = self.ports_per_vlan[vlan][src_idx]
        dst_port = self.ports_per_vlan[vlan][dst_idx]
        src_addr = self.host_ip(vlan_ip_range, src_idx)
        dst_addr = self.host_ip(vlan_ip_range, dst_idx)
        src_mac = self.hex_to_mac(self.vlan_host_map[src_port][src_addr])
        packet = simple_arp_packet(
            eth_src=src_mac, arp_op=1, ip_snd=src_addr, ip_tgt=dst_addr, hw_snd=src_mac)
        expect = simple_arp_packet(
            eth_dst=src_mac, arp_op=2, ip_snd=dst_addr, ip_tgt=src_addr, hw_tgt=src_mac)
        self.log("ARP ping: src idx %d port %d mac %s addr %s" %
                 (src_idx, src_port, src_mac, src_addr))
        self.log("ARP ping: dst idx %d port %d addr %s" %
                 (dst_idx, dst_port, dst_addr))
        self.arp_ping = bytes(packet)
        self.arp_resp = Mask(expect)
        self.arp_resp.set_do_not_care_scapy(scapy.Ether, 'src')
        self.arp_resp.set_do_not_care(*self.calc_offset_and_size(expect, scapy.ARP, "hwsrc"))
        self.arp_src_port = src_port

    def generate_arp_vlan_gw_packets(self):
        self.arp_vlan_gw_ping_packets = []

        for src_port in self.active_port_indices if self.is_dualtor else self.vlan_host_ping_map:
            src_addr = random.choice(list(self.vlan_host_ping_map[src_port].keys()))
            src_mac = self.hex_to_mac(
                self.vlan_host_ping_map[src_port][src_addr])
            packet = simple_arp_packet(eth_src=src_mac,
                                       arp_op=1,
                                       ip_snd=src_addr,
                                       ip_tgt="192.168.0.1",  # TODO: make this dynamic
                                       hw_snd=src_mac)

            self.arp_vlan_gw_ping_packets.append((src_port, bytes(packet)))

        exp_packet = simple_arp_packet(pktlen=42, eth_src=self.vlan_mac,
                                       arp_op=2,
                                       ip_snd="192.168.0.1",
                                       hw_snd=self.vlan_mac)
        self.arp_vlan_gw_ping_exp_packet = Mask(exp_packet, ignore_extra_bytes=True)
        self.arp_vlan_gw_ping_exp_packet.set_do_not_care_scapy(scapy.Ether, 'dst')
        # PTF's field size calculation is broken for dynamic length fields, do it ourselves
        self.arp_vlan_gw_ping_exp_packet.set_do_not_care(*self.calc_offset_and_size(exp_packet, scapy.ARP, "pdst"))
        self.arp_vlan_gw_ping_exp_packet.set_do_not_care(*self.calc_offset_and_size(exp_packet, scapy.ARP, "hwdst"))

        exp_packet = simple_arp_packet(pktlen=42, eth_src=self.vlan_mac,
                                       arp_op=2,
                                       ip_snd="192.168.0.1",
                                       hw_snd=self.vlan_mac)
        exp_packet = exp_packet / ("fe11e1" * 6)
        self.arp_vlan_gw_ferret_exp_packet = Mask(exp_packet)
        self.arp_vlan_gw_ferret_exp_packet.set_do_not_care_scapy(scapy.Ether, 'dst')
        # PTF's field size calculation is broken for dynamic length fields, do it ourselves
        self.arp_vlan_gw_ferret_exp_packet.set_do_not_care(*self.calc_offset_and_size(exp_packet, scapy.ARP, "pdst"))
        self.arp_vlan_gw_ferret_exp_packet.set_do_not_care(*self.calc_offset_and_size(exp_packet, scapy.ARP, "hwdst"))

    def put_nowait(self, queue, data):
        try:
            queue.put_nowait(data)
        except Queue.Full:
            pass

    def pre_reboot_test_setup(self):
        self.reboot_start = None
        self.no_routing_start = None
        self.no_routing_stop = None
        self.no_control_start = None
        self.no_control_stop = None
        self.no_cp_replies = None
        self.upper_replies = []
        self.routing_always = False
        self.total_disrupt_packets = None
        self.total_disrupt_time = None
        self.ssh_jobs = []
        self.lacp_session_pause = dict()
        for addr in self.ssh_targets:
            q = Queue.Queue(1)
            self.lacp_session_pause[addr] = None
            thr = threading.Thread(target=self.peer_state_check, kwargs={
                                   'ip': addr, 'queue': q})
            thr.setDaemon(True)
            self.ssh_jobs.append((thr, q))
            thr.start()

        if self.setup_fdb_before_test:
            self.log("Run some server traffic to populate FDB table...")
            self.setup_fdb()

        self.log("Starting reachability state watch thread...")
        self.watching = True
        self.light_probe = False
        # Waiter Event for the Watcher state is stopped.
        self.watcher_is_stopped = threading.Event()
        # Waiter Event for the Watcher state is running.
        self.watcher_is_running = threading.Event()
        # By default the Watcher is not running.
        self.watcher_is_stopped.set()
        # By default its required to wait for the Watcher started.
        self.watcher_is_running.clear()
        # Give watch thread some time to wind up
        watcher = self.pool.apply_async(self.reachability_watcher)      # noqa: F841
        time.sleep(5)

    def get_warmboot_finalizer_state(self):
        self.log("get the finalizer_state with: 'sudo systemctl is-active warmboot-finalizer.service'")
        stdout, stderr, _ = self.dut_connection.execCommand(
            'sudo systemctl is-active warmboot-finalizer.service')
        if stderr:
            self.fails['dut'].add("Error collecting Finalizer state. stderr: {}, stdout:{}".format(
                str(stderr), str(stdout)))
            self.log("Error collecting Finalizer state. stderr: {}, stdout:{}".format(str(stderr), str(stdout)))
            raise Exception("Error collecting Finalizer state. stderr: {}, stdout:{}".format(
                str(stderr), str(stdout)))
        if not stdout:
            self.log('Finalizer state not returned from DUT')
            return ''

        finalizer_state = stdout[0].strip()
        self.log("The returned finalizer_state is {}".format(finalizer_state))
        return finalizer_state

    def get_now_time(self):
        stdout, stderr, _ = self.dut_connection.execCommand(
            'date +"%Y-%m-%d %H:%M:%S"')
        if stderr:
            self.fails['dut'].add("Error collecting current date from DUT. stderr: {}, stdout:{}".format(
                str(stderr), str(stdout)))
            raise Exception("Error collecting current date from DUT. stderr: {}, stdout:{}".format(
                str(stderr), str(stdout)))
        if not stdout:
            self.fails['dut'].add(
                'Error collecting current date from DUT: empty value returned')
            raise Exception(
                'Error collecting current date from DUT: empty value returned')
        return datetime.datetime.strptime(stdout[0].strip(), "%Y-%m-%d %H:%M:%S")

    def check_warmboot_finalizer(self, finalizer_timeout):
        self.wait_until_control_plane_up()
        dut_datetime = self.get_now_time()
        self.log('waiting for warmboot-finalizer service to become activating')
        self.finalizer_state = self.get_warmboot_finalizer_state()

        while self.finalizer_state != 'activating':
            time.sleep(1)
            dut_datetime_after_ssh = self.get_now_time()
            time_passed = float(dut_datetime_after_ssh.strftime(
                "%s")) - float(dut_datetime.strftime("%s"))
            if time_passed > finalizer_timeout:
                self.fails['dut'].add(
                    'warmboot-finalizer never reached state "activating"')
                self.log('TimeoutError: warmboot-finalizer never reached state "activating"')
                raise TimeoutError
            self.finalizer_state = self.get_warmboot_finalizer_state()

        self.log('waiting for warmboot-finalizer service to finish')
        self.finalizer_state = self.get_warmboot_finalizer_state()
        self.log('warmboot finalizer service state {}'.format(self.finalizer_state))
        count = 0
        while self.finalizer_state != 'inactive':
            try:
                self.finalizer_state = self.get_warmboot_finalizer_state()
            except Exception:
                traceback_msg = traceback.format_exc()
                self.log("Exception happened during get warmboot finalizer service state: {}".format(traceback_msg))
                raise

            self.log('warmboot finalizer service state {}'.format(self.finalizer_state))
            time.sleep(10)
            if count * 10 > int(self.test_params['warm_up_timeout_secs']):
                self.fails['dut'].add(
                    'warmboot-finalizer.service did not finish')
                self.log('TimeoutError: warmboot-finalizer.service did not finish')
                raise TimeoutError
            count += 1
        self.log('warmboot-finalizer service finished')

    def wait_until_control_plane_down(self):
        self.log("Wait until Control plane is down")
        self.timeout(self.wait_until_cpu_port_down, self.control_plane_down_timeout,
                     "DUT hasn't shutdown in {} seconds".format(self.control_plane_down_timeout))
        if self.reboot_type == 'fast-reboot':
            self.light_probe = True
        else:
            # add or del routes during boot
            self.do_inboot_oper()
        self.reboot_start = datetime.datetime.now()
        self.log("Dut reboots: reboot start %s" % str(self.reboot_start))

    def wait_until_control_plane_up(self):
        self.log("Wait until Control plane is up")
        self.timeout(self.wait_until_cpu_port_up, self.task_timeout,
                     "DUT hasn't come back up in {} seconds".format(self.task_timeout))
        self.no_control_stop = datetime.datetime.now()
        self.log("Dut reboots: control plane up at %s" %
                 str(self.no_control_stop))

    def wait_until_service_restart(self):
        self.log("Wait until sevice restart")
        self.reboot_start = datetime.datetime.now()
        service_set = set(self.test_params['service_list'])
        wait_time = 120
        while wait_time > 0:
            for service_name in self.test_params['service_list']:
                if service_name not in service_set:
                    continue
                cmd = 'systemctl show -p ExecMainStartTimestamp {}'.format(
                    service_name)
                stdout, _, _ = self.dut_connection.execCommand(cmd)
                if self.service_data[service_name]['service_start_time'] != str(stdout[0]).strip():
                    service_set.remove(service_name)
            if not service_set:
                break
            wait_time -= 10
            time.sleep(10)

        if service_set:
            self.fails['dut'].add("Container {} hasn't come back up in {} seconds".format(
                ','.join(service_set), wait_time))
            raise TimeoutError

        # TODO: add timestamp
        self.log("Service has restarted")

    def handle_advanced_reboot_health_check(self):
        self.log("Check that device is still forwarding data plane traffic")
        self.fails['dut'].add(
            "Data plane has a forwarding problem after CPU went down")
        self.check_alive()
        self.fails['dut'].clear()

        # wait until sniffer and sender threads have started
        while not (self.sniff_thr.is_alive() and self.sender_thr.is_alive()):
            time.sleep(1)

        self.log("IO sender and sniffer threads have started, wait until completion")
        self.sniff_thr.join()
        self.sender_thr.join()

        # Stop watching DUT
        self.watching = False
        self.log("Stopping reachability state watch thread.")
        # Wait for the Watcher stopped.
        self.watcher_is_stopped.wait(timeout=10)

        examine_start = datetime.datetime.now()
        self.log("Packet flow examine started %s after the reboot" %
                 str(examine_start - self.reboot_start))
        self.examine_flow()
        self.log("Packet flow examine finished after %s" %
                 str(datetime.datetime.now() - examine_start))

        if self.lost_packets:
            self.no_routing_stop, self.no_routing_start = datetime.datetime.fromtimestamp(
                self.no_routing_stop), datetime.datetime.fromtimestamp(self.no_routing_start)
            self.log("The longest disruption lasted %.3f seconds. %d packet(s) lost." % (
                self.max_disrupt_time, self.max_lost_id))
            self.log("Total disruptions count is %d. All disruptions lasted %.3f seconds. Total %d packet(s) lost" %
                     (self.disrupts_count, self.total_disrupt_time, self.total_disrupt_packets))
        else:
            self.no_routing_start = self.reboot_start
            self.no_routing_stop = self.reboot_start

    def handle_post_reboot_health_check(self):
        # wait until all bgp session are established
        self.log("Wait until bgp routing is up on all devices")
        for _, q in self.ssh_jobs:
            q.put('quit')

        def wait_for_ssh_threads(signal):
            while any(thr.is_alive() for thr, _ in self.ssh_jobs) and not signal.is_set():
                self.log('Waiting till SSH threads stop')
                time.sleep(self.TIMEOUT)

            for thr, _ in self.ssh_jobs:
                thr.join()

        self.timeout(wait_for_ssh_threads, self.task_timeout,
                     "SSH threads haven't finished for %d seconds" % self.task_timeout)

        self.log("Data plane works again. Start time: %s" %
                 str(self.no_routing_stop))
        self.log("")

        if self.no_routing_stop - self.no_routing_start > self.limit:
            self.fails['dut'].add("Longest downtime period must be less then %s seconds. It was %s"
                                  % (self.test_params['reboot_limit_in_seconds'],
                                     str(self.no_routing_stop - self.no_routing_start)))
        if self.no_routing_stop - self.reboot_start > datetime.timedelta(seconds=self.test_params['graceful_limit']):
            self.fails['dut'].add("%s cycle must be less than graceful limit %s seconds" % (
                self.reboot_type, self.test_params['graceful_limit']))

        if self.total_disrupt_time > self.limit.total_seconds():
            self.fails['dut'].add("Total downtime period must be less then %s seconds. It was %s"
                                  % (str(self.limit), str(self.total_disrupt_time)))

        if 'warm-reboot' in self.reboot_type:
            # after the data plane is up, check for routing changes
            if self.test_params['inboot_oper'] and self.sad_handle:
                self.check_inboot_sad_status()
            # postboot check for all preboot operations
            if self.test_params['preboot_oper'] and self.sad_handle:
                self.check_postboot_sad_status()
            else:
                # verify there are no interface flaps after warm boot
                self.neigh_lag_status_check()

        if 'service-warm-restart' == self.reboot_type:
            # verify there are no interface flaps after warm boot
            self.neigh_lag_status_check()

    def handle_advanced_reboot_health_check_kvm(self):
        self.log("Wait until data plane stops")
        forward_stop_signal = multiprocessing.Event()
        async_forward_stop = self.pool.apply_async(
            self.check_forwarding_stop, args=(forward_stop_signal,))

        self.log("Wait until control plane up")
        port_up_signal = multiprocessing.Event()
        async_cpu_up = self.pool.apply_async(
            self.wait_until_cpu_port_up, args=(port_up_signal,))

        try:
            self.no_routing_start, _ = async_forward_stop.get(
                timeout=self.task_timeout)
            self.log("Data plane was stopped, Waiting until it's up. Stop time: %s" % str(
                self.no_routing_start))
        except TimeoutError:
            forward_stop_signal.set()
            self.log("Data plane never stop")

        try:
            async_cpu_up.get(timeout=self.task_timeout)
            no_control_stop = self.cpu_state.get_state_time('up')
            self.log("Control plane down stops %s" % str(no_control_stop))
        except TimeoutError:
            port_up_signal.set()
            self.log("DUT hasn't bootup in %d seconds" % self.task_timeout)
            self.fails['dut'].add(
                "DUT hasn't booted up in %d seconds" % self.task_timeout)
            raise

        # Wait until data plane up if it stopped
        if self.no_routing_start is not None:
            self.no_routing_stop, _ = self.timeout(self.check_forwarding_resume,
                                                   self.task_timeout,
                                                   "DUT hasn't started to work for %d seconds" % self.task_timeout)
        else:
            self.no_routing_stop = datetime.datetime.min
            self.no_routing_start = datetime.datetime.min

        # Stop watching DUT
        self.watching = False

    def handle_post_reboot_health_check_kvm(self):
        # wait until all bgp session are established
        self.log("Wait until bgp routing is up on all devices")
        for _, q in self.ssh_jobs:
            q.put('quit')

        def wait_for_ssh_threads(signal):
            while any(thr.is_alive() for thr, _ in self.ssh_jobs) and not signal.is_set():
                time.sleep(self.TIMEOUT)

            for thr, _ in self.ssh_jobs:
                thr.join()

        self.timeout(wait_for_ssh_threads, self.task_timeout,
                     "SSH threads haven't finished for %d seconds" % self.task_timeout)

        self.log("Data plane works again. Start time: %s" %
                 str(self.no_routing_stop))
        self.log("")

        if self.no_routing_stop - self.no_routing_start > self.limit:
            self.fails['dut'].add("Longest downtime period must be less then %s seconds. It was %s"
                                  % (self.test_params['reboot_limit_in_seconds'],
                                     str(self.no_routing_stop - self.no_routing_start)))
        if self.no_routing_stop - self.reboot_start > datetime.timedelta(seconds=self.test_params['graceful_limit']):
            self.fails['dut'].add("%s cycle must be less than graceful limit %s seconds" % (
                self.reboot_type, self.test_params['graceful_limit']))

    def handle_post_reboot_test_reports(self):
        # Stop watching DUT
        self.watching = False
        # revert to pretest state
        if self.sad_oper and self.sad_handle:
            self.sad_revert()
            if self.test_params['inboot_oper']:
                self.check_postboot_sad_status()
            self.log(" ")

        # Generating report
        self.log("="*50)
        self.log("Report:")
        self.log("="*50)

        self.log("LACP/BGP were down for (extracted from cli):")
        self.log("-"*50)
        for ip in sorted(self.cli_info.keys()):
            self.log("    %s - lacp: %7.3f (%d) po_events: (%d) bgp v4: %7.3f (%d) bgp v6: %7.3f (%d)"
                     % (ip, self.cli_info[ip]['lacp'][1],   self.cli_info[ip]['lacp'][0],
                        self.cli_info[ip]['po'][1],
                        self.cli_info[ip]['bgp_v4'][1], self.cli_info[ip]['bgp_v4'][0],
                        self.cli_info[ip]['bgp_v6'][1], self.cli_info[ip]['bgp_v6'][0]))

        self.log("-"*50)
        self.log("Extracted from VM logs:")
        self.log("-"*50)
        for ip in sorted(self.logs_info.keys()):
            self.log("Extracted log info from %s" % ip)
            for msg in sorted(self.logs_info[ip].keys()):
                if msg not in ['error', 'route_timeout']:
                    self.log("    %s : %d" % (msg, self.logs_info[ip][msg]))
                else:
                    self.log("    %s" % self.logs_info[ip][msg])
            self.log("-"*50)

        self.log("Summary:")
        self.log("-"*50)

        if self.no_routing_stop:
            self.log("Longest downtime period was %s" %
                     str(self.no_routing_stop - self.no_routing_start))
            reboot_time = "0:00:00" if self.routing_always else str(
                self.no_routing_stop - self.reboot_start)
            self.log("Reboot time was %s" % reboot_time)
            self.log("Expected downtime is less then %s" % self.limit)

        if self.reboot_type == 'fast-reboot' and self.no_cp_replies:
            self.log("How many packets were received back when control plane was down: %d Expected: %d" % (
                self.no_cp_replies, self.nr_vl_pkts))

        has_info = any(len(info) > 0 for info in self.info.values())
        if has_info:
            self.log("-"*50)
            self.log("Additional info:")
            self.log("-"*50)
            for name, info in self.info.items():
                for entry in info:
                    self.log("INFO:%s:%s" % (name, entry))
            self.log("-"*50)

        is_good = all(len(fails) == 0 for fails in self.fails.values())

        errors = ""
        if not is_good:
            self.log("-"*50)
            self.log("Fails:")
            self.log("-"*50)

            errors = "\n\nSomething went wrong. Please check output below:\n\n"
            for name, fails in self.fails.items():
                for fail in fails:
                    self.log("FAILED:%s:%s" % (name, fail))
                    errors += "FAILED:%s:%s\n" % (name, fail)

        self.log("="*50)

        if self.no_routing_stop and self.no_routing_start:
            dataplane_downtime = (self.no_routing_stop -
                                  self.no_routing_start).total_seconds()
        else:
            dataplane_downtime = ""
        if self.total_disrupt_time:
            # Add total downtime (calculated in physical warmboot test using packet disruptions)
            dataplane_downtime = self.total_disrupt_time
        dataplane_report = dict()
        dataplane_report["checked_successfully"] = self.dataplane_loss_checked_successfully
        dataplane_report["downtime"] = str(dataplane_downtime)
        dataplane_report["lost_packets"] = str(self.total_disrupt_packets) \
            if self.total_disrupt_packets is not None else ""
        controlplane_report = dict()

        if self.no_control_stop and self.no_control_start:
            controlplane_downtime = (
                self.no_control_stop - self.no_control_start).total_seconds()
        else:
            controlplane_downtime = ""
        controlplane_report["downtime"] = str(controlplane_downtime)
        controlplane_report["arp_ping"] = ""  # TODO
        controlplane_report["lacp_sessions"] = self.lacp_session_pause
        self.report["dataplane"] = dataplane_report
        self.report["controlplane"] = controlplane_report
        with open(self.report_file_name, 'w') as reportfile:
            json.dump(self.report, reportfile)

        self.assertTrue(is_good, errors)

    def runTest(self):
        # Set LACP timer multiplier for cEOS peers when it is not default (3)
        if self.test_params['neighbor_type'] == "eos" and self.test_params['ceos_neighbor_lacp_multiplier'] != 3:
            self.ceos_set_lacp_all_neighs(self.test_params['ceos_neighbor_lacp_multiplier'])

        self.pre_reboot_test_setup()
        try:
            self.log("Check that device is alive and pinging")
            self.fails['dut'].add("DUT is not ready for test")
            self.wait_dut_to_warm_up()
            self.fails['dut'].clear()

            self.clear_dut_counters()
            self.log("Schedule to reboot the remote switch in %s sec" %
                     self.reboot_delay)
            thr = threading.Thread(target=self.reboot_dut)
            thr.setDaemon(True)
            thr.start()
            if self.reboot_type != 'service-warm-restart':
                self.wait_until_control_plane_down()
                self.no_control_start = self.cpu_state.get_state_time('down')
            else:
                self.wait_until_service_restart()

            if 'warm-reboot' in self.reboot_type or 'fast-reboot' in self.reboot_type:
                finalizer_timeout = 60 + \
                    self.test_params['reboot_limit_in_seconds']
                thr = threading.Thread(target=self.check_warmboot_finalizer,
                                       kwargs={'finalizer_timeout': finalizer_timeout})
                thr.setDaemon(True)
                thr.start()
                self.warmboot_finalizer_thread = thr

            if self.kvm_test:
                self.handle_advanced_reboot_health_check_kvm()
                self.handle_post_reboot_health_check_kvm()
            else:
                self.handle_advanced_reboot_health_check()
                self.handle_post_reboot_health_check()

            if 'warm-reboot' in self.reboot_type or 'fast-reboot' in self.reboot_type:
                total_timeout = finalizer_timeout + \
                    self.test_params['warm_up_timeout_secs']
                start_time = datetime.datetime.now()
                # Wait until timeout happens OR the IO test completes
                while ((datetime.datetime.now() - start_time).seconds < total_timeout) and\
                        self.warmboot_finalizer_thread.is_alive():
                    time.sleep(0.5)
                if self.warmboot_finalizer_thread.is_alive():
                    self.fails['dut'].add("Warmboot Finalizer hasn't finished for {} seconds. Finalizer state: {}"
                                          .format(total_timeout, self.get_warmboot_finalizer_state()))

            # Check sonic version after reboot
            self.check_sonic_version_after_reboot()
        except Exception:
            traceback_msg = traceback.format_exc()
            self.fails['dut'].add(traceback_msg)
        finally:
            # Restore cEOS LACP timer multiplier to default (3)
            if self.test_params['neighbor_type'] == "eos" and self.test_params['ceos_neighbor_lacp_multiplier'] != 3:
                self.ceos_set_lacp_all_neighs(3)

            self.handle_post_reboot_test_reports()

    def ceos_set_lacp_all_neighs(self, multiplier):
        for neigh in self.ssh_targets:
            self.neigh_handle = HostDevice.getHostDeviceInstance(
                                    self.test_params['neighbor_type'], neigh, None, self.test_params)
            self.neigh_handle.connect()

            raw_json = self.neigh_handle.do_cmd("show lacp interface | json")
            neigh_int_json = json.loads(raw_json[raw_json.find("{"):raw_json.rfind("}")+1])

            self.neigh_handle.do_cmd("config")
            for lag in neigh_int_json["portChannels"]:
                for neigh_int in neigh_int_json["portChannels"][lag]['interfaces']:
                    self.neigh_handle.do_cmd(f"interface {neigh_int}")
                    self.neigh_handle.do_cmd(f"lacp timer multiplier {multiplier}")

            self.neigh_handle.disconnect()

    def neigh_lag_status_check(self):
        """
        Ensure there are no interface flaps after warm-boot
        """
        for neigh in self.ssh_targets:
            flap_cnt = None
            if self.test_params['neighbor_type'] == "sonic":
                flap_cnt = self.cli_info[neigh]['po'][1]
            else:
                self.test_params['port_channel_intf_idx'] = [x['ptf_ports'][0] for x in self.vm_dut_map.values()
                                                             if x['mgmt_addr'] == neigh]
                self.neigh_handle = HostDevice.getHostDeviceInstance(self.test_params['neighbor_type'], neigh,
                                                                     None, self.test_params)
                self.neigh_handle.connect()
                fails, flap_cnt = self.neigh_handle.verify_neigh_lag_no_flap()
                self.neigh_handle.disconnect()
                self.fails[neigh] |= fails
            if not flap_cnt:
                self.log("No LAG flaps seen on %s after warm boot" % neigh)
            else:
                self.fails[neigh].add(
                    "LAG flapped %s times on %s after warm boot" % (flap_cnt, neigh))

    def check_sonic_version_after_reboot(self):
        # Check sonic version after reboot
        target_version = self.test_params['target_version']
        if target_version:
            stdout, stderr, return_code = self.dut_connection.execCommand(
                "sudo sonic_installer list | grep Current | awk '{print $2}'")
            current_version = ""
            if stdout != []:
                current_version = str(stdout[0]).replace('\n', '')
            self.log("Current={} Target={}".format(
                current_version, target_version))
            if current_version != target_version:
                raise Exception("Sonic upgrade failed. Target={} Current={}".format(
                    target_version, current_version))

    def extract_no_cpu_replies(self, arr):
        """
        This function tries to extract number of replies from dataplane, when control plane is non working
        """
        # remove all tail zero values
        non_zero = filter(lambda x: x > 0, arr)

        # check that last value is different from previos
        if len(non_zero) > 1 and non_zero[-1] < non_zero[-2]:
            return non_zero[-2]
        else:
            return non_zero[-1]

    def get_teamd_state(self):
        self.log("Start to Get the teamd state")
        stdout, stderr, _ = self.dut_connection.execCommand(
            'sudo systemctl is-active teamd.service')
        if stderr:
            self.fails['dut'].add("Error collecting teamd state. stderr: {}, stdout:{}".format(
                str(stderr), str(stdout)))
            self.log("Error collecting teamd state. stderr: {}, stdout:{}".format(
                str(stderr), str(stdout)))
            raise Exception("Error collecting teamd state. stderr: {}, stdout:{}".format(
                str(stderr), str(stdout)))
        if not stdout:
            self.log('teamd state not returned from DUT')
            return ''

        teamd_state = stdout[0].strip()
        self.log("The teamd state is: {}".format(teamd_state))
        return teamd_state

    def get_installed_sonic_version(self):
        stdout, _, _ = self.dut_connection.execCommand(
            "sudo sonic_installer list | grep Current | awk '{print $2}'")
        return stdout[0]

    def wait_until_teamd_goes_down(self):
        self.log('Waiting for teamd service to go down')
        teamd_state = self.get_teamd_state()
        self.log('teamd service state: {}'.format(teamd_state))
        dut_datetime = self.get_now_time()
        teamd_shutdown_timeout = 300

        while teamd_state == 'active':
            time.sleep(1)
            try:
                dut_datetime_during_shutdown = self.get_now_time()
            except Exception:
                traceback_msg = traceback.format_exc()
                self.log("Exception happened during get dut time: {}".format(traceback_msg))
                continue
            time_passed = float(dut_datetime_during_shutdown.strftime(
                "%s")) - float(dut_datetime.strftime("%s"))
            if time_passed > teamd_shutdown_timeout:
                self.fails['dut'].add(
                    'Teamd service did not go down')
                self.log('TimeoutError: Teamd service did not go down')
                raise TimeoutError
            try:
                teamd_state = self.get_teamd_state()
            except Exception:
                traceback_msg = traceback.format_exc()
                self.log("Exception happened during get teamd state: {}".format(traceback_msg))
                raise

        self.log('teamd service state: {}'.format(teamd_state))

    def reboot_dut(self):
        time.sleep(self.reboot_delay)

        self.log("Rebooting remote side")
        if self.reboot_type != 'service-warm-restart' and self.test_params['other_vendor_flag'] is False:
            # Check to see if the warm-reboot script knows about the retry count feature
            stdout, stderr, return_code = self.dut_connection.execCommand(
                "sudo " + self.reboot_type + " -h", timeout=5)
            # 202205 image doesn't support retry count feature despite the fact it is present in the cli output
            if "retry count" in stdout and '202205' not in self.installed_sonic_version:
                if self.test_params['neighbor_type'] == "sonic":
                    reboot_command = self.reboot_type + " -N"
                else:
                    reboot_command = self.reboot_type + " -n"
            else:
                reboot_command = self.reboot_type

            # create an empty log file to capture output of reboot command
            reboot_log_file = "/host/{}.log".format(reboot_command.replace(' ', ''))
            self.dut_connection.execCommand("sudo touch {}; sudo chmod 666 {}".format(
                reboot_log_file, reboot_log_file))

            # execute reboot command w/ nohup so that when the execCommand times-out:
            # 1. there is a reader/writer for any bash commands using PIPE
            # 2. the output and error of CLI still gets written to log file
            stdout, stderr, return_code = self.dut_connection.execCommand(
                "nohup sudo {} -v &> {}".format(
                    reboot_command, reboot_log_file), timeout=10)

        elif self.test_params['other_vendor_flag'] is True:
            ignore_db_integrity_check = " -d"
            stdout, stderr, return_code = self.dut_connection.execCommand(
                "sudo " + self.reboot_type + ignore_db_integrity_check, timeout=10)

        else:
            self.restart_service()
            return

        if not self.kvm_test and\
                (self.reboot_type == 'fast-reboot' or 'warm-reboot' in
                 self.reboot_type or 'service-warm-restart' in self.reboot_type):
            # Event for the sniff_in_background status.
            self.sniffer_started = threading.Event()

            self.wait_until_teamd_goes_down()

            self.sniff_thr.start()
            self.sender_thr.start()

        if stdout != []:
            self.log("stdout from %s: %s" % (self.reboot_type, str(stdout)))
        if stderr != []:
            self.log("stderr from %s: %s" % (self.reboot_type, str(stderr)))
            self.fails['dut'].add(
                "{} failed with error {}".format(self.reboot_type, stderr))
            thread.interrupt_main()
            raise Exception("{} failed with error {}".format(
                self.reboot_type, stderr))
        self.log("return code from %s: %s" %
                 (self.reboot_type, str(return_code)))

        # Note: a timeout reboot in ssh session will return a 255 code
        if return_code not in [0, 255]:
            thread.interrupt_main()

        return

    def restart_service(self):
        for service_name in self.test_params['service_list']:
            if 'image_path_on_dut' in self.service_data[service_name]:
                stdout, stderr, return_code = self.dut_connection.execCommand(
                    "sudo sonic-installer upgrade-docker {} {} -y --warm"
                    .format(service_name, self.service_data[service_name]['image_path_on_dut']), timeout=30)
            else:
                self.dut_connection.execCommand(
                    'sudo config warm_restart enable {}'.format(service_name))
                self.pre_service_warm_restart(service_name)
                stdout, stderr, return_code = self.dut_connection.execCommand(
                    'sudo service {} restart'.format(service_name))

            if stdout != []:
                self.log("stdout from %s %s: %s" %
                         (self.reboot_type, service_name, str(stdout)))
            if stderr != []:
                self.log("stderr from %s %s: %s" %
                         (self.reboot_type, service_name, str(stderr)))
                self.fails['dut'].add(
                    "service warm restart {} failed with error {}".format(service_name, stderr))
                thread.interrupt_main()
                raise Exception("{} failed with error {}".format(
                    self.reboot_type, stderr))
            self.log("return code from %s %s: %s" %
                     (self.reboot_type, service_name, str(return_code)))
            if return_code not in [0, 255]:
                thread.interrupt_main()

    def pre_service_warm_restart(self, service_name):
        """
        Copy from src/sonic-utilities/sonic_installer/main.py to do some special operation for particular containers
        """
        if service_name == 'swss':
            cmd = 'docker exec -i swss orchagent_restart_check -w 2000 -r 5'
            stdout, stderr, return_code = self.dut_connection.execCommand(cmd)
            if return_code != 0:
                self.log('stdout from {}: {}'.format(cmd, str(stdout)))
                self.log('stderr from {}: {}'.format(cmd, str(stderr)))
                self.log(
                    'orchagent is not in clean state, RESTARTCHECK failed: {}'.format(return_code))
        elif service_name == 'bgp':
            self.dut_connection.execCommand(
                'docker exec -i bgp pkill -9 zebra')
            self.dut_connection.execCommand('docker exec -i bgp pkill -9 bgpd')
        elif service_name == 'teamd':
            self.dut_connection.execCommand(
                'docker exec -i teamd pkill -USR1 teamd > /dev/null')

    def cmd(self, cmds):
        process = subprocess.Popen(cmds,
                                   shell=False,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        return_code = process.returncode

        return stdout, stderr, return_code

    def peer_state_check(self, ip, queue):
        self.log('SSH thread for VM {} started'.format(ip))
        self.test_params['port_channel_intf_idx'] = [x['ptf_ports'][0] for x in self.vm_dut_map.values()
                                                     if x['mgmt_addr'] == ip]
        ssh = HostDevice.getHostDeviceInstance(self.test_params['neighbor_type'], ip, queue,
                                               self.test_params, log_cb=self.log)
        try:
            self.fails[ip], self.info[ip], self.cli_info[ip], self.logs_info[ip], self.lacp_pdu_times[ip] = ssh.run()
        except Exception:
            traceback_msg = traceback.format_exc()
            self.log("Error in HostDevice: {}".format(traceback_msg))
            self.fails[ip] = set()
            self.fails[ip].add("HostDevice hit an exception")
            self.info[ip] = set()
            self.cli_info[ip] = {
                    "lacp": [0, 0],
                    "po": [0, 0],
                    "bgp_v4": [0, 0],
                    "bgp_v6": [0, 0],
                    }
            self.logs_info[ip] = {}
            self.lacp_pdu_times[ip] = {
                    "lacp_all": []
                    }
        self.log('SSH thread for VM {} finished'.format(ip))

        lacp_pdu_times = self.lacp_pdu_times[ip]
        lacp_pdu_all_times = lacp_pdu_times.get("lacp_all")

        self.log('lacp_pdu_all_times: IP:{}: {}'.format(ip, lacp_pdu_all_times))

        # in the list of all LACPDUs received by T1, find the largest time gap between two consecutive LACPDUs
        max_lacp_session_wait = None
        max_allowed_lacp_session_wait = 150
        if lacp_pdu_all_times and len(lacp_pdu_all_times) > 1:
            lacp_pdu_all_times.sort()
            max_lacp_session_wait = 0
            prev_time = lacp_pdu_all_times[0]
            for new_time in lacp_pdu_all_times[1:]:
                lacp_session_wait = new_time - prev_time
                if lacp_session_wait > max_lacp_session_wait:
                    max_lacp_session_wait = lacp_session_wait
                prev_time = new_time

        if 'warm-reboot' in self.reboot_type:
            if max_lacp_session_wait and max_lacp_session_wait >= max_allowed_lacp_session_wait and not self.kvm_test:
                self.fails['dut'].add("LACP session likely terminated by neighbor ({})".format(ip) +
                                      " post-reboot lacpdu came after {}s of lacpdu pre-boot"
                                      .format(max_lacp_session_wait))
            elif not max_lacp_session_wait and not self.kvm_test:
                self.fails['dut'].add("LACP session timing not captured")

        self.lacp_session_pause[ip] = max_lacp_session_wait

    def wait_until_cpu_port_down(self, signal):
        while not signal.is_set():
            for _, q in self.ssh_jobs:
                self.put_nowait(q, 'cpu_going_down')
            if self.cpu_state.get() == 'down':
                for _, q in self.ssh_jobs:
                    q.put('cpu_down')
                break
            time.sleep(self.TIMEOUT)

    def wait_until_cpu_port_up(self, signal):
        while not signal.is_set():
            for _, q in self.ssh_jobs:
                self.put_nowait(q, 'cpu_going_up')
            if self.cpu_state.get() == 'up':
                for _, q in self.ssh_jobs:
                    q.put('cpu_up')
                break
            time.sleep(self.TIMEOUT)

    def apply_filter_all_ports(self, filter_expression):
        for p in self.dataplane.ports.values():
            port = p.get_packet_source()
            attach_filter(port.socket, filter_expression, port.interface_name)

    def send_in_background(self, packets_list=None):
        """
        This method sends predefined list of packets with predefined interval.
        """
        if not packets_list:
            packets_list = self.packets_list
        self.sniffer_started.wait(timeout=10)
        with self.dataplane_io_lock:
            # While running fast data plane sender thread there are two reasons for filter to be applied
            #  1. filter out data plane traffic which is tcp to free up the load
            #     on PTF socket (sniffer thread is using a different one)
            #  2. during warm neighbor restoration DUT will send a lot of ARP requests which we are not interested in
            # This is essential to get stable results
            self.apply_filter_all_ports(
                'not (arp and ether src {} and ether dst ff:ff:ff:ff:ff:ff) and not tcp'.format(
                    self.test_params['dut_mac']))
            sender_start = datetime.datetime.now()
            self.log("Sender started at %s" % str(sender_start))

            self.packets_list = []
            from_t1_iter = itertools.cycle(self.from_t1)
            sent_count_vlan_to_t1 = 0
            sent_count_t1_to_vlan = 0

            while True:
                time.sleep(self.send_interval)
                if self.reboot_start and self.finalizer_state == "inactive":
                    # keep sending packets until device reboots and finalizer enters inactive state
                    break
                payload = '0' * 60 + str(self.sent_packet_count)
                if (self.sent_packet_count % 5) == 0:   # From vlan to T1.
                    from_port, packet = next(self.watcher_from_server_iter)
                    packet = scapyall.Ether(packet)
                    packet.load = payload
                    sent_count_vlan_to_t1 += 1
                else:   # From T1 to vlan.
                    src_port, packet = next(from_t1_iter)
                    packet = scapyall.Ether(packet)
                    packet.load = payload
                    from_port = src_port
                    sent_count_t1_to_vlan += 1
                testutils.send_packet(self, from_port, bytes(packet))
                self.sent_packet_count = self.sent_packet_count + 1

            self.log("Sent count vlan to t1: {}".format(sent_count_vlan_to_t1))
            self.log("Sent count t1 to vlan: {}".format(sent_count_t1_to_vlan))
            self.log("Sender has been running for %s" %
                     str(datetime.datetime.now() - sender_start))
            self.log("Total sent packets by sender: {}".format(self.sent_packet_count))

            # Signal sniffer thread to allow early finish.
            # Without this signalling mechanism, the sniffer thread can continue for a hardcoded max time.
            # Sometimes this max time is too long and sniffer keeps running too long after sender finishes.
            # Other times, sniffer finishes too early (when max time is less)
            # while the sender is still sending packets.
            # So now:
            # 1. sniffer max timeout is increased (to prevent sniffer finish before sender)
            # 2. and sender can signal sniffer to end after all packets are sent.
            time.sleep(1)
            self.kill_sniffer = True

    def sniff_in_background(self, wait=None):
        """
        This function listens on all ports, in both directions, for the TCP src=1234 dst=5000 packets, until timeout.
        Once found, all packets are dumped to local pcap file,
        and all packets are saved to self.packets as scapy type(pcap format).
        """
        if not wait:
            wait = self.time_to_listen + self.test_params['sniff_time_incr']
        sniffer_start = datetime.datetime.now()
        self.log("Sniffer started at %s" % str(sniffer_start))
        sniff_filter = "tcp and tcp dst port 5000 and tcp src port 1234 and not icmp"
        sniffer = threading.Thread(target=self.tcpdump_sniff, kwargs={
                                   'wait': wait, 'sniff_filter': sniff_filter})
        sniffer.start()
        # Let the scapy sniff initialize completely.
        time.sleep(2)
        sniffer.join()
        self.log("Sniffer has been running for %s" %
                 str(datetime.datetime.now() - sniffer_start))
        self.sniffer_started.clear()

    def tcpdump_sniff(self, wait=300, sniff_filter=''):
        """
        @summary: PTF runner -  runs a sniffer in PTF container.
        Args:
            wait (int): Duration in seconds to sniff the traffic
            sniff_filter (str): Filter that tcpdump will use to collect only relevant packets
        """
        try:
            capture_pcap = ("/tmp/capture_%s.pcapng" % self.logfile_suffix
                            if self.logfile_suffix is not None else "/tmp/capture.pcapng")
            subprocess.call(["rm", "-rf", capture_pcap])  # remove old capture
            self.kill_sniffer = False
            self.start_sniffer(capture_pcap, sniff_filter, wait)
            self.packets = scapyall.rdpcap(capture_pcap)
            self.log("Number of all packets captured: {}".format(len(self.packets)))
        except Exception:
            traceback_msg = traceback.format_exc()
            self.log("Error in tcpdump_sniff: {}".format(traceback_msg))

    def start_sniffer(self, pcap_path, tcpdump_filter, timeout):
        """
        Start tcpdump sniffer on all data interfaces, and kill them after a specified timeout
        """
        self.tcpdump_data_ifaces = [
            iface for iface in scapyall.get_if_list() if iface.startswith('eth')]
        process_args = ['dumpcap', '-w', pcap_path, '-f', tcpdump_filter, '-Z', 'none', '-s', '1514', '-t']
        for iface in self.tcpdump_data_ifaces:
            process_args += ['-i', iface]

        process = subprocess.Popen(process_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.log('Dumpcap sniffer process started')

        pcap_existence_check_limit = 10
        pcap_existence_check_count = 0
        while not os.path.exists(pcap_path) and pcap_existence_check_count < pcap_existence_check_limit:
            time.sleep(1)
            pcap_existence_check_count += 1

        if not os.path.exists(pcap_path):
            self.log("Dumpcap did not create pcap file!")
            process.terminate()
            process.kill()
            return

        # Unblock waiter for the send_in_background.
        self.sniffer_started.set()

        time_start = time.time()
        while not self.kill_sniffer:
            time.sleep(1)
            curr_time = time.time()
            if curr_time - time_start > timeout:
                break

        self.log("Going to kill dumpcap process by SIGTERM")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        # Return code here could be 0, so we need to explicitly check for None
        if process.returncode is not None:
            self.log("Dumpcap process terminated")
            return

        self.log("Killing dumpcap process")
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Return code here could be 0, so we need to explicitly check for None
        if process.returncode is not None:
            self.log("Dumpcap process killed")

    def check_tcp_payload(self, packet):
        """
        This method is used by examine_flow() method.
        It returns True if a packet is not corrupted and has a valid TCP sequential TCP Payload
        """
        try:
            int(bytes(packet[scapyall.TCP].payload)
                ) in range(self.sent_packet_count)
            return True
        except Exception:
            return False

    def no_flood(self, packet):
        """
        This method filters packets which are unique (i.e. no floods).
        """
        if (not int(bytes(packet[scapyall.TCP].payload)) in self.unique_id) and \
                (packet[scapyall.Ether].src == self.dut_mac or packet[scapyall.Ether].src == self.vlan_mac):
            # This is a unique (no flooded) received packet.
            # for dualtor, t1->server rcvd pkt will have src MAC as vlan_mac,
            # and server->t1 rcvd pkt will have src MAC as dut_mac
            self.unique_id.append(int(bytes(packet[scapyall.TCP].payload)))
            return True
        elif packet[scapyall.Ether].dst == self.dut_mac or packet[scapyall.Ether].dst == self.vlan_mac:
            # This is a sent packet.
            # for dualtor, t1->server sent pkt will have dst MAC as dut_mac,
            # and server->t1 sent pkt will have dst MAC as vlan_mac
            return True
        else:
            return False

    def examine_flow(self, filename=None):
        """
        This method examines pcap file (if given), or self.packets scapy file.
        The method compares TCP payloads of the packets one by one (assuming all payloads are consecutive integers),
        and the losses if found - are treated as disruptions in Dataplane forwarding.
        All disruptions are saved to self.lost_packets dictionary, in format:
        disrupt_start_id = (missing_packets_count, disrupt_time, disrupt_start_timestamp, disrupt_stop_timestamp)
        """
        if filename:
            all_packets = scapyall.rdpcap(filename)
        elif self.packets:
            all_packets = self.packets
        else:
            self.log("Filename and self.packets are not defined.")
            self.fails['dut'].add("Filename and self.packets are not defined")
            return None
        # Filter out packets and remove floods:
        # This list will contain all unique Payload ID, to filter out received floods.
        self.unique_id = list()
        filtered_packets = [pkt for pkt in all_packets if
                            scapyall.TCP in pkt and
                            scapyall.ICMP not in pkt and
                            pkt[scapyall.TCP].sport == 1234 and
                            pkt[scapyall.TCP].dport == 5000 and
                            self.check_tcp_payload(pkt) and
                            self.no_flood(pkt)
                            ]

        if self.vnet:
            decap_packets = [scapyall.Ether(bytes(pkt.payload.payload.payload)[8:]) for pkt in all_packets if
                             scapyall.UDP in pkt and
                             pkt[scapyall.UDP].sport == 1234
                             ]
            filtered_decap_packets = [pkt for pkt in decap_packets if
                                      scapyall.TCP in pkt and
                                      scapyall.ICMP not in pkt and
                                      pkt[scapyall.TCP].sport == 1234 and
                                      pkt[scapyall.TCP].dport == 5000 and
                                      self.check_tcp_payload(pkt) and
                                      self.no_flood(pkt)
                                      ]
            filtered_packets = filtered_packets + filtered_decap_packets

        # Re-arrange packets, if delayed, by Payload ID and Timestamp:
        packets = sorted(filtered_packets, key=lambda packet: (
            int(bytes(packet[scapyall.TCP].payload)), packet.time))
        self.lost_packets = dict()
        self.max_disrupt, self.total_disruption = 0, 0
        sent_packets = dict()
        # Track packet id's that were neither sent or received
        missing_sent_and_received_packet_id_sequences = []
        self.fails['dut'].add("Sniffer failed to capture any traffic")
        self.assertTrue(packets, "Sniffer failed to capture any traffic")
        self.fails['dut'].clear()
        prev_payload = None
        if packets:
            prev_payload, prev_time = -1, 0
            sent_payload = 0
            received_counter = 0    # Counts packets from dut.
            received_but_not_sent_packets = set()
            sent_counter = 0
            received_t1_to_vlan = 0
            received_vlan_to_t1 = 0
            missed_vlan_to_t1 = 0
            missed_t1_to_vlan = 0
            flooded_pkts = []
            self.disruption_start, self.disruption_stop = None, None
            for packet in packets:
                if packet[scapyall.Ether].dst == self.dut_mac or packet[scapyall.Ether].dst == self.vlan_mac:
                    # This is a sent packet - keep track of it as payload_id:timestamp.
                    # for dualtor both MACs are needed:
                    #   t1->server sent pkt will have dst MAC as dut_mac,
                    #   and server->t1 sent pkt will have dst MAC as vlan_mac
                    sent_payload = int(bytes(packet[scapyall.TCP].payload))
                    if sent_payload in sent_packets:
                        flooded_pkts.append(sent_payload)
                    sent_packets[sent_payload] = float(packet.time)
                    sent_counter += 1
                    continue
                if packet[scapyall.Ether].src == self.dut_mac or packet[scapyall.Ether].src == self.vlan_mac:
                    # This is a received packet.
                    # for dualtor both MACs are needed:
                    #   t1->server rcvd pkt will have src MAC as vlan_mac,
                    #   and server->t1 rcvd pkt will have src MAC as dut_mac
                    received_time = packet.time
                    received_payload = int(bytes(packet[scapyall.TCP].payload))
                    if (received_payload % 5) == 0:   # From vlan to T1.
                        received_vlan_to_t1 += 1
                    else:
                        received_t1_to_vlan += 1
                    received_counter += 1
                if not (received_payload and received_time):
                    # This is the first valid received packet.
                    prev_payload = received_payload
                    prev_time = received_time
                    continue
                if received_payload - prev_payload > 1:
                    if received_payload not in sent_packets:
                        self.log("Ignoring received packet with payload {}, as it was not sent".format(
                            received_payload))
                        received_but_not_sent_packets.add(received_payload)
                        continue
                    # Packets in a row are missing, a potential disruption.
                    self.log("received_payload: {}, prev_payload: {}, sent_counter: {}, received_counter: {}".format(
                        received_payload, prev_payload, sent_counter, received_counter))
                    # How many packets lost in a row.
                    lost_id = (received_payload - 1) - prev_payload

                    # Find previous sequential sent packet that was captured
                    missing_sent_and_received_pkt_count = 0
                    prev_pkt_pt = prev_payload + 1
                    prev_sent_packet_time = None
                    while prev_pkt_pt < received_payload:
                        if prev_pkt_pt in sent_packets:
                            prev_sent_packet_time = sent_packets[prev_pkt_pt]
                            break  # Found it
                        else:
                            if prev_pkt_pt not in received_but_not_sent_packets:
                                missing_sent_and_received_pkt_count += 1
                            prev_pkt_pt += 1
                    if missing_sent_and_received_pkt_count > 0:
                        missing_sent_and_received_packet_id_sequences_fmtd = \
                            str(prev_payload + 1) if missing_sent_and_received_pkt_count == 1\
                            else "{}-{}".format(prev_payload + 1, received_payload - 1)
                        missing_sent_and_received_packet_id_sequences.append(
                            missing_sent_and_received_packet_id_sequences_fmtd)
                    if prev_sent_packet_time is not None:
                        # Disruption occurred - some sent packets were not received

                        # How long disrupt lasted.
                        this_sent_packet_time = sent_packets[received_payload]
                        disrupt = this_sent_packet_time - prev_sent_packet_time

                        # Add disrupt to the dict:
                        self.lost_packets[prev_payload] = (
                            lost_id, disrupt, received_time - disrupt, received_time)
                        self.log("Disruption between packet ID %d and %d. For %.4f " % (
                            prev_payload, received_payload, disrupt))
                        for lost_index in range(prev_payload + 1, received_payload):
                            # lost received for packet sent from vlan to T1.
                            if lost_index in sent_packets:
                                if (lost_index % 5) == 0:
                                    missed_vlan_to_t1 += 1
                                else:
                                    missed_t1_to_vlan += 1
                        self.log("")
                        if not self.disruption_start:
                            self.disruption_start = datetime.datetime.fromtimestamp(
                                prev_time)
                        self.disruption_stop = datetime.datetime.fromtimestamp(
                            received_time)
                prev_payload = received_payload
                prev_time = received_time
            self.log(
                "**************** Packet received summary: ********************")
            self.log("*********** Sent packets captured - {}".format(sent_counter))
            self.log("*********** received packets captured - t1-to-vlan - {}".format(received_t1_to_vlan))
            self.log("*********** received packets captured - vlan-to-t1 - {}".format(received_vlan_to_t1))
            self.log("*********** Missed received packets - t1-to-vlan - {}".format(missed_t1_to_vlan))
            self.log("*********** Missed received packets - vlan-to-t1 - {}".format(missed_vlan_to_t1))
            self.log("*********** Flooded pkts - {}".format(flooded_pkts))
            self.log("**************************************************************")
        self.fails['dut'].add("Sniffer failed to filter any traffic from DUT")
        self.assertTrue(received_counter,
                        "Sniffer failed to filter any traffic from DUT")
        self.fails['dut'].clear()
        self.disrupts_count = len(self.lost_packets)  # Total disrupt counter.
        if self.lost_packets:
            # Find the longest loss with the longest time:
            max_disrupt_from_id, (self.max_lost_id, self.max_disrupt_time,
                                  self.no_routing_start, self.no_routing_stop) = \
                max(self.lost_packets.items(), key=lambda item: item[1][0:2])
            self.total_disrupt_packets = sum(
                [item[0] for item in self.lost_packets.values()])
            self.total_disrupt_time = sum(
                [item[1] for item in self.lost_packets.values()])
            self.log("Disruptions happen between %s and %s after the reboot." %
                     (str(self.disruption_start - self.reboot_start), str(self.disruption_stop - self.reboot_start)))
        else:
            self.max_lost_id = 0
            self.max_disrupt_time = 0
            self.total_disrupt_packets = 0
            self.total_disrupt_time = 0
            self.log("Gaps in forwarding not found.")

        if missing_sent_and_received_packet_id_sequences:
            self.fails["infrastructure"].add(
                "Missing sent and received packets: {}"
                .format(missing_sent_and_received_packet_id_sequences))

        self.dataplane_loss_checked_successfully = True

        if self.reboot_type == "fast-reboot" and not self.lost_packets:
            self.dataplane_loss_checked_successfully = False
            self.fails["dut"].add("Data traffic loss not found but reboot test type is '%s' which "
                                  "must have data traffic loss" % self.reboot_type)

        if self.sent_packet_count > sent_counter:
            self.dataplane_loss_checked_successfully = False
            self.fails["dut"].add("Not all sent packets counted by receiver process. "
                                  "Could be issue with sniffer performance")

        total_validation_packets = received_t1_to_vlan + \
            received_vlan_to_t1 + missed_t1_to_vlan + missed_vlan_to_t1
        # In some cases DUT may flood original packet to all members of VLAN, we do check that we do not flood too much
        allowed_number_of_flooded_original_packets = 250
        if (sent_counter - total_validation_packets) > allowed_number_of_flooded_original_packets:
            self.dataplane_loss_checked_successfully = False
            self.fails["dut"].add("Unexpected count of sent packets available in pcap file. "
                                  "Could be issue with DUT flooding for original packets which was sent to DUT, "
                                  "flooded count is: {}".format(sent_counter - total_validation_packets))

        if prev_payload != (self.sent_packet_count - 1):
            # Specific case when packet loss started but final lost packet not detected
            self.dataplane_loss_checked_successfully = False
            message = "Unable to calculate the dataplane traffic loss time. The traffic did not restore after " \
                      "performing reboot for the pre-defined test checker period. Note: the traffic could possibly " \
                      "restore after too long time, this could be checked manually."
            self.log(message)
            self.fails["dut"].add(message)

        self.log("Total incoming packets captured %d" % received_counter)
        if packets:
            filename = ('/tmp/capture_filtered.pcap' if self.logfile_suffix is None
                        else "/tmp/capture_filtered_%s.pcap" % self.logfile_suffix)
            scapyall.wrpcap(filename, packets)
            self.log("Filtered pcap dumped to %s" % filename)

    def check_forwarding_stop(self, signal):
        self.asic_start_recording_vlan_reachability()

        while not signal.is_set():
            state = self.asic_state.get()
            for _, q in self.ssh_jobs:
                self.put_nowait(q, 'check_stop')
            if state == 'down':
                break
            time.sleep(self.TIMEOUT)

        self.asic_stop_recording_vlan_reachability()
        return self.asic_state.get_state_time(state), self.get_asic_vlan_reachability()

    def check_forwarding_resume(self, signal):
        while not signal.is_set():
            state = self.asic_state.get()
            if state != 'down':
                break
            time.sleep(self.TIMEOUT)

        return self.asic_state.get_state_time(state), self.get_asic_vlan_reachability()

    def ping_data_plane(self, light_probe=True):
        self.dataplane.flush()
        replies_from_servers = self.pingFromServers()
        if replies_from_servers > 0 or not light_probe:
            replies_from_upper = self.pingFromUpperTier()
        else:
            replies_from_upper = 0

        return replies_from_servers, replies_from_upper

    def wait_dut_to_warm_up(self):
        # When the DUT is freshly rebooted, it appears that it needs to warm
        # up towards PTF docker. In practice, I've seen this warm up taking
        # up to ~70 seconds.

        fail = None

        dut_stabilize_secs = int(self.test_params['dut_stabilize_secs'])
        warm_up_timeout_secs = int(self.test_params['warm_up_timeout_secs'])

        start_time = datetime.datetime.now()
        up_time = None

        # First wait until DUT data/control planes are up
        while True:
            dataplane = self.asic_state.get()
            ctrlplane = self.cpu_state.get()
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            if dataplane == 'up' and ctrlplane == 'up':
                if not up_time:
                    up_time = datetime.datetime.now()
                up_secs = (datetime.datetime.now() - up_time).total_seconds()
                if up_secs > dut_stabilize_secs:
                    break
            else:
                # reset up_time
                up_time = None

            if elapsed > warm_up_timeout_secs:
                raise Exception("IO didn't come up within warm up timeout. Control plane: {}, Data plane: {}."
                                "Actual warm up time {}".format(ctrlplane, dataplane, elapsed))
            time.sleep(1)

        # check until flooding is over. Flooding happens when FDB entry of
        # certain host is not yet learnt by the ASIC, therefore it sends
        # packet to all vlan ports.
        uptime = datetime.datetime.now()
        while True:
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            if not self.asic_state.is_flooding() and elapsed > dut_stabilize_secs:
                break
            if elapsed > warm_up_timeout_secs:
                if self.allow_vlan_flooding:
                    break
                raise Exception(
                    "Data plane didn't stop flooding within warm up timeout")
            time.sleep(1)

        dataplane = self.asic_state.get()
        ctrlplane = self.cpu_state.get()
        if not dataplane == 'up':
            fail = "Data plane"
        elif not ctrlplane == 'up':
            fail = "Control plane"

        if fail is not None:
            raise Exception(
                "{} went down while waiting for flooding to stop".format(fail))

        if self.asic_state.get_state_time('up') > uptime:
            fail = "Data plane"
        elif self.cpu_state.get_state_time('up') > uptime:
            fail = "Control plane"

        if fail is not None:
            raise Exception(
                "{} flapped while waiting for the warm up".format(fail))

        # Everything is good

    def clear_dut_counters(self):
        # Clear the counters after the WARM UP is complete
        # this is done so that drops can be accurately calculated
        # after reboot test is finished
        clear_counter_cmds = ["sonic-clear counters",
                              "sonic-clear queuecounters",
                              "sonic-clear dropcounters",
                              "sonic-clear rifcounters",
                              "sonic-clear pfccounters"
                              ]
        if 'broadcom' in self.test_params['asic_type']:
            clear_counter_cmds.append("bcmcmd 'clear counters'")
        for cmd in clear_counter_cmds:
            self.dut_connection.execCommand(cmd)

    def check_alive(self):
        # This function checks that DUT routes the packets in the both directions.
        #
        # Sometimes first attempt failes because ARP responses to DUT are not so fast.
        # But after this the function expects to see steady "replies".
        # If the function sees that there is an issue with the dataplane after we saw
        # successful replies it considers that the DUT is not healthy
        #
        # Sometimes I see that DUT returns more replies then requests.
        # I think this is because of not populated FDB table
        # The function waits while it's done

        uptime = None
        for counter in range(self.nr_tests * 2):
            state = self.asic_state.get()
            if state == 'up':
                if not uptime:
                    uptime = self.asic_state.get_state_time(state)
            else:
                if uptime:
                    raise Exception("Data plane stopped working")
            time.sleep(2)

        # wait, until FDB entries are populated
        for _ in range(self.nr_tests * 10):  # wait for some time
            if self.asic_state.is_flooding():
                time.sleep(2)
            else:
                break
        else:
            raise Exception("DUT is flooding")

    def get_asic_vlan_reachability(self):
        return self.asic_vlan_reach

    def asic_start_recording_vlan_reachability(self):
        with self.vlan_lock:
            self.asic_vlan_reach = []
            self.recording = True

    def asic_stop_recording_vlan_reachability(self):
        with self.vlan_lock:
            self.recording = False

    def try_record_asic_vlan_recachability(self, t1_to_vlan):
        with self.vlan_lock:
            if self.recording:
                self.asic_vlan_reach.append(t1_to_vlan)

    def log_asic_state_change(self, reachable, partial=False, t1_to_vlan=0, flooding=False):
        old = self.asic_state.get()

        if reachable:
            state = 'up' if not partial else 'partial'
        else:
            state = 'down'

        self.try_record_asic_vlan_recachability(t1_to_vlan)

        self.asic_state.set_flooding(flooding)

        if old != state:
            self.log("Data plane state transition from %s to %s (%d)" %
                     (old, state, t1_to_vlan))
            self.asic_state.set(state)

    def log_cpu_state_change(self, reachable, partial=False, flooding=False):
        old = self.cpu_state.get()

        if reachable:
            state = 'up' if not partial else 'partial'
        else:
            state = 'down'

        self.cpu_state.set_flooding(flooding)

        if old != state:
            self.log("Control plane state transition from %s to %s" %
                     (old, state))
            self.cpu_state.set(state)

    def log_vlan_state_change(self, reachable):
        old = self.vlan_state.get()

        if reachable:
            state = 'up'
        else:
            state = 'down'

        if old != state:
            self.log("VLAN ARP state transition from %s to %s" % (old, state))
            self.vlan_state.set(state)

    def log_vlan_gw_state_change(self, reachable, partial=False, flooding=False):
        old = self.vlan_gw_state.get()

        if reachable:
            state = 'up' if not partial else 'partial'
        else:
            state = 'down'

        self.vlan_gw_state.set_flooding(flooding)

        if old != state:
            self.log("VLAN GW state transition from %s to %s" %
                     (old, state))
            self.vlan_gw_state.set(state)

    def reachability_watcher(self):
        # This function watches the reachability of the CPU port, and ASIC. It logs the state
        # changes for future analysis
        self.log('Reachability watcher started')
        self.watcher_is_stopped.clear()  # Watcher is running.
        while self.watching:
            self.log('Reachability watcher - checking data plane')
            if self.dataplane_io_lock.acquire(False):
                vlan_to_t1, t1_to_vlan = self.ping_data_plane(self.light_probe)
                reachable = (t1_to_vlan > self.nr_vl_pkts * 0.7 and
                             vlan_to_t1 > self.nr_pc_pkts * 0.7)
                partial = (reachable and
                           (t1_to_vlan < self.nr_vl_pkts or
                            vlan_to_t1 < self.nr_pc_pkts))
                flooding = (reachable and
                            (t1_to_vlan > self.nr_vl_pkts or
                             vlan_to_t1 > self.nr_pc_pkts))
                self.log_asic_state_change(
                    reachable, partial, t1_to_vlan, flooding)
                self.dataplane_io_lock.release()
            else:
                self.log("Reachability watcher - Dataplane is busy. Skipping the check")

            self.log('Reachability watcher - checking control plane')
            total_rcv_pkt_cnt = self.pingDut()
            reachable = total_rcv_pkt_cnt > 0 and total_rcv_pkt_cnt > self.ping_dut_pkts * 0.7
            partial = total_rcv_pkt_cnt > 0 and total_rcv_pkt_cnt < self.ping_dut_pkts
            flooding = reachable and total_rcv_pkt_cnt > self.ping_dut_pkts
            self.log_cpu_state_change(reachable, partial, flooding)
            total_rcv_pkt_cnt = self.arpPing()
            reachable = total_rcv_pkt_cnt >= self.arp_ping_pkts
            self.log_vlan_state_change(reachable)

            self.log('Reachability watcher - checking VLAN GW IP')
            total_rcv_pkt_cnt = self.arpVlanGwPing()
            reachable = total_rcv_pkt_cnt > 0 and total_rcv_pkt_cnt > self.arp_vlan_gw_ping_pkts * 0.7
            partial = total_rcv_pkt_cnt > 0 and total_rcv_pkt_cnt < self.arp_vlan_gw_ping_pkts
            flooding = reachable and total_rcv_pkt_cnt > self.arp_vlan_gw_ping_pkts
            self.log_vlan_gw_state_change(reachable, partial, flooding)

            self.watcher_is_running.set()   # Watcher is running.
        self.log('Reachability watcher stopped')
        self.watcher_is_stopped.set()       # Watcher has stopped.
        self.watcher_is_running.clear()     # Watcher has stopped.

    def pingFromServers(self):
        for _ in range(self.nr_pc_pkts):
            entry = next(self.watcher_from_server_iter)
            testutils.send_packet(self, *entry)
        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(
            self, self.from_vlan_exp_packet, self.from_server_dst_ports, timeout=self.PKT_TOUT)

        self.log("Send %5d Received %5d servers->t1" %
                 (self.nr_pc_pkts, total_rcv_pkt_cnt), True)

        return total_rcv_pkt_cnt

    def pingFromUpperTier(self):
        for entry in self.from_t1:
            testutils.send_packet(self, *entry)

        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(
            self, self.from_t1_exp_packet, self.vlan_ports, timeout=self.PKT_TOUT)

        self.log("Send %5d Received %5d t1->servers" %
                 (self.nr_vl_pkts, total_rcv_pkt_cnt), True)

        return total_rcv_pkt_cnt

    def pingDut(self):
        if "allow_mac_jumping" in self.test_params and self.test_params['allow_mac_jumping']:
            for i in range(self.ping_dut_pkts):
                testutils.send_packet(self, self.random_port(
                    self.vlan_ports), self.ping_dut_macjump_packet)
        else:
            for i in range(self.ping_dut_pkts):
                src_port, packet = random.choice(self.ping_dut_packets)
                testutils.send_packet(self, src_port, packet)

        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(
            self, self.ping_dut_exp_packet, self.vlan_ports, timeout=self.PKT_TOUT)

        self.log("Send %5d Received %5d ping DUT" %
                 (self.ping_dut_pkts, total_rcv_pkt_cnt), True)

        return total_rcv_pkt_cnt

    def arpPing(self):
        for i in range(self.arp_ping_pkts):
            testutils.send_packet(self, self.arp_src_port, self.arp_ping)
        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(
            self, self.arp_resp, [self.arp_src_port], timeout=self.PKT_TOUT)
        self.log("Send %5d Received %5d arp ping" %
                 (self.arp_ping_pkts, total_rcv_pkt_cnt), True)
        return total_rcv_pkt_cnt

    def arpVlanGwPing(self):
        total_rcv_pkt_cnt = 0
        packets = random.sample(self.arp_vlan_gw_ping_packets, self.arp_vlan_gw_ping_pkts)
        for packet in packets:
            src_port, arp_packet = packet
            testutils.send_packet(self, src_port, arp_packet)
        total_rcv_pkt_cnt = testutils.count_matched_packets_all_ports(
            self, self.arp_vlan_gw_ping_exp_packet, self.vlan_ports, timeout=self.PKT_TOUT)
        self.log("Send %5d Received %5d arp vlan gw ping" %
                 (self.arp_vlan_gw_ping_pkts, total_rcv_pkt_cnt), True)
        return total_rcv_pkt_cnt
