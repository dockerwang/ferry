# Copyright 2014 OpenCore LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import ferry.install
from ferry.docker.docker import DockerInstance, DockerCLI
import importlib
import inspect
import json
import logging
import re
from subprocess import Popen, PIPE
import time
import yaml

class OpenStackFabric(object):

    def __init__(self, config=None, bootstrap=False):
        self.name = "openstack"
        self.repo = 'public'

        self.config = config
        self._init_openstack(self.config)

        self.bootstrap = bootstrap
        self.cli = DockerCLI()
        self.cli.key = self._get_host_key()
        self.docker_user = self.cli.docker_user
        self.inspector = OpenStackInspector(self)

    def _load_class(self, class_name):
        """
        Dynamically load a class
        """
        s = class_name.split("/")
        module_path = s[0]
        clazz_name = s[1]
        module = importlib.import_module(module_path)
        for n, o in inspect.getmembers(module):
            if inspect.isclass(o):
                if o.__module__ == module_path and o.__name__ == clazz_name:
                    return o(self, self.config)
        return None

    def _init_openstack(self, conf_file):
        with open(conf_file, 'r') as f:
            args = yaml.load(f)

            # The actual OpenStack launcher. This lets us customize 
            # launching into different OpenStack environments that each
            # may be slightly different (HP Cloud, Rackspace, etc). 
            launcher = args["system"]["mode"]
            self.launcher = self._load_class(launcher)

            # The name of the data network device (eth*). 
            self.data_network = args["system"]["network"]

            # Determine if we are using this fabric in proxy
            # mode. Proxy mode means that the client is external
            # to the network, but controller has direct access. 
            self.proxy = bool(args["system"]["proxy"])

    def _get_host_key(self):
        return "/ferry/keys/" + self.launcher.ssh_key + ".pem"

    def version(self):
        """
        Fetch the current docker version.
        """
        return "0.1"

    def get_fs_type(self):
        """
        Get the filesystem type associated with docker. 
        """
        return "xfs"

    def restart(self, cluster_uuid, service_uuid, containers):
        """
        Restart the stopped containers.
        """
        # First need to restart all the virtual machines.
        logging.warning("RESTARTING VMs")
        addrs = self.launcher._restart_stack(cluster_uuid, service_uuid)
        
        # Then need to restart Ferry on all the hosts. 
        logging.warning("RESTARTING FERRY")
        cmd = "ferry server"
        for ip in addrs:
            output, err = self.cmd_raw(self.cli.key, ip, cmd, self.launcher.ssh_user)
            logging.warning("RESTART OUT: " + str(output))
            logging.warning("RESTART ERR: " + str(err))

        # Finally, restart the stopped containers. 
        logging.warning("RESTARTING CONTAINERS")
        cmd = "cat /service/sconf/container.pid"
        for c in containers:
            # Before restarting the containers, we need to learn their
            # container IDs. It should be stored on a cidfile. 
            output, err = self.cmd_raw(self.cli.key, c.external_ip, cmd, self.launcher.ssh_user)
            c.container = output.strip()
            logging.warning("RESTART CONTAINER " + c.container)
            logging.warning("CONTAINER ERR: " + str(err))
            self.cli.start(image = c.image,
                           container = c.container, 
                           service_type = c.service_type,
                           keydir = c.keydir,
                           keyname = c.keyname,
                           privatekey = c.privatekey,
                           volumes = c.volumes,
                           args = c.args,
                           server = c.external_ip, 
                           user = self.launcher.ssh_user,
                           inspector = self.inspector,
                           background = True)
        return containers

    def _copy_public_keys(self, container, server):
        """
        Copy over the public ssh key to the server so that we can start the
        container correctly. 
        """

        keydir = container['keydir'].values()[0]
        self.copy_raw(key = self.cli.key,
                      ip = server, 
                      from_dir = keydir + "/" + container["keyname"], 
                      to_dir = "/ferry/keys/",
                      user = self.launcher.ssh_user)

    def execute_docker_containers(self, cinfo, lxc_opts, private_ip, public_ip):
        """
        Run the Docker container and use the OpenStack inspector to get information
        about the container/VM.
        """

        host_map = None
        host_map_keys = []
        mounts = {}
        cinfo['default_cmd'] = "/service/sbin/startnode init"
        container = self.cli.run(service_type = cinfo['type'], 
                                 image = cinfo['image'], 
                                 volumes = cinfo['volumes'],
                                 keydir = { '/service/keys' : '/ferry/keys' }, 
                                 keyname = cinfo['keyname'], 
                                 privatekey = cinfo['privatekey'], 
                                 open_ports = host_map_keys,
                                 host_map = host_map, 
                                 expose_group = cinfo['exposed'], 
                                 hostname = cinfo['hostname'],
                                 default_cmd = cinfo['default_cmd'],
                                 args= cinfo['args'],
                                 lxc_opts = lxc_opts,
                                 server = public_ip,
                                 user = self.launcher.ssh_user, 
                                 inspector = self.inspector,
                                 background = True)
        if container:
            container.internal_ip = private_ip
            if self.proxy:
                # When the fabric controller is acting in proxy mode, 
                # it can contact the VMs via their private addresses. 
                container.external_ip = private_ip
            else:
                # Otherwise, the controller can only interact with the
                # VMs via their public IP address. 
                container.external_ip = public_ip

            container.default_user = self.cli.docker_user

            if 'name' in cinfo:
                container.name = cinfo['name']

            if 'volume_user' in cinfo:
                mounts[container] = {'user':cinfo['volume_user'],
                                     'vols':cinfo['volumes'].items()}

            return container, mounts
        else:
            return None, None


    def alloc(self, cluster_uuid, service_uuid, container_info, ctype):
        """
        Allocate a new cluster. 
        """
        return self.launcher.alloc(cluster_uuid, service_uuid, container_info, ctype, self.proxy)

    def stop(self, cluster_uuid, service_uuid, containers):
        """
        Stop the running containers
        """
        self._remove(cluster_uuid, service_uuid, containers)

    def halt(self, cluster_uuid, service_uuid, containers):
        """
        Safe stop the containers. 
        """

        # Stop the containers in the VMs. Stopping the container
        # should jump us back out to the host. Afterwards, quit
        # ferry so that we can restart later. 
        halt = '/service/sbin/startnode halt'
        ferry = 'ferry quit'
        for c in containers:
            self.cmd_raw(c.privatekey, c.external_ip, halt, c.default_user)
            self.cmd_raw(self.cli.key, c.external_ip, ferry, self.launcher.ssh_user)

        # Now go ahead and stop the VMs. 
        self.launcher._stop_stack(cluster_uuid, service_uuid)

    def remove(self, cluster_uuid, service_uuid, containers):
        """
        Remove the running instances
        """
        self.launcher._delete_stack(cluster_uuid, service_uuid)

    def copy(self, containers, from_dir, to_dir):
        """
        Copy over the contents to each container
        """
        for c in containers:
            self.copy_raw(c.privatekey, c.external_ip, from_dir, to_dir, c.default_user)

    def copy_raw(self, key, ip, from_dir, to_dir, user):
        opts = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
        scp = 'scp ' + opts + ' -i ' + key + ' -r ' + from_dir + ' ' + user + '@' + ip + ':' + to_dir
        logging.warning(scp)

        # All the possible errors that might happen when
        # we try to connect via ssh. 
        conn_closed = re.compile('.*Connection closed.*', re.DOTALL)
        timed_out = re.compile('.*timed out*', re.DOTALL)
        permission = re.compile('.*Permission denied.*', re.DOTALL)
        while(True):
            proc = Popen(scp, stdout=PIPE, stderr=PIPE, shell=True)
            output = proc.stdout.read()
            err = proc.stderr.read()
            if conn_closed.match(err) or timed_out.match(err) or permission.match(err):
                logging.warning("COPY ERROR, TRY AGAIN")
                time.sleep(3)
            else:
                break

    def cmd(self, containers, cmd):
        """
        Run a command on all the containers and collect the output. 
        """
        all_output = {}
        for c in containers:
            output, _ = self.cmd_raw(c.privatekey, c.external_ip, cmd, c.default_user)
            all_output[c.host_name] = output.strip()
        return all_output

    def cmd_raw(self, key, ip, cmd, user):
        ip = user + '@' + ip
        ssh = 'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i ' + key + ' -t -t ' + ip + ' \'%s\'' % cmd
        logging.warning(ssh)

        # All the possible errors that might happen when
        # we try to connect via ssh. 
        conn_closed = re.compile('.*Connection closed.*', re.DOTALL)
        timed_out = re.compile('.*timed out*', re.DOTALL)
        permission = re.compile('.*Permission denied.*', re.DOTALL)
        while(True):
            proc = Popen(ssh, stdout=PIPE, stderr=PIPE, shell=True)
            output = proc.stdout.read()
            err = proc.stderr.read()
            if conn_closed.match(err) or timed_out.match(err) or permission.match(err):
                logging.warning("SSH ERROR, TRY AGAIN")
                time.sleep(3)
            else:
                break
        return output, err

class OpenStackInspector(object):
    def __init__(self, fabric):
        self.fabric = fabric

    def inspect(self, image, container, keydir=None, keyname=None, privatekey=None, volumes=None, hostname=None, open_ports=[], host_map=None, service_type=None, args=None, server=None):
        """
        Inspect a container and return information on how
        to connect to the container. 
        """        
        instance = DockerInstance()

        # We don't keep track of the container ID in single-network
        # mode, so use this to store the VM image instead. 
        # instance.container = self.fabric.launcher.default_image
        instance.container = container

        # The port mapping should be 1-to-1 since we're using
        # the physical networking mode. 
        instance.ports = {}
        for p in open_ports:
            instance.ports[p] = { 'HostIp' : '0.0.0.0',
                                  'HostPort' : p }

        # These values are just stored for convenience. 
        instance.image = image
        instance.host_name = hostname
        instance.service_type = service_type
        instance.args = args
        instance.volumes = volumes
        instance.keydir = keydir
        instance.keyname = keyname
        instance.privatekey = privatekey

        return instance        
    
