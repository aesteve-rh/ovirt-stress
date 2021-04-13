import argparse
import logging
import subprocess
import threading
import time
import uuid
import yaml

from collections import Counter

import ovirtsdk4 as sdk
import ovirtsdk4.types as types

log = logging.getLogger("test")


class Timeout(Exception):
    pass


class JobError(Exception):
    pass


class Runner:

    def __init__(self, index, conf):
        self.index = index
        self.conf = conf
        self.connection = None
        self.iteration = None
        self.vm = None
        self.snapshot = None
        self.passed = 0
        self.failed = 0
        self.errored = 0

    def run(self):
        log.info("Started")
        self.connect()

        for i in range(self.conf["iterations"]):
            self.iteration = i
            start = time.monotonic()
            log.info("Iteration %d started", i)

            try:
                self.setup()
                try:
                    self.test()
                    log.info("Iteration %d passed", i)
                    self.passed += 1
                except Exception:
                    log.exception("Iteration %d failed", i)
                    self.failed += 1
            except Exception:
                log.exception("Iteration %d errored", i)
                self.errored += 1
            finally:
                try:
                    self.teardown()
                except Exception:
                    log.exception("Error tearing down")

            log.info("Iteration %d completed in %d seconds",
                     i, time.monotonic() - start)

        self.disconnect()
        log.info("Finished")

    def setup(self):
        self.vm = None
        self.snapshot = None
        self.check_data_center()
        self.create_vm()
        self.start_vm()
        self.create_snapshot()
        self.write_data()

    def test(self):
        self.remove_snapshot()

    def teardown(self):
        if self.vm:
            self.check_data_center()
            self.stop_vm()
            self.remove_vm()

    def connect(self):
        self.connection = sdk.Connection(
            url="https://{}/ovirt-engine/api".format(self.conf["engine_fqdn"]),
            username=self.conf["engine_username"],
            password=self.conf["engine_password"],
            ca_file=self.conf["engine_cafile"]
        )

    def disconnect(self):
        self.connection.close()

    # Data center health

    def check_data_center(self):
        log.info("Checking data center status for cluster %s",
                 self.conf["cluster_name"])

        start = time.monotonic()
        deadline = start + self.conf["data_center_up_timeout"]

        system_service = self.connection.system_service()

        clusters_service = system_service.clusters_service()
        clusters = clusters_service.list(
            search="name={}".format(self.conf["cluster_name"]))

        cluster = clusters[0]

        data_centers_service = system_service.data_centers_service()
        data_center_service = data_centers_service.data_center_service(
            id=cluster.data_center.id)

        data_center = data_center_service.get()
        log.debug("Data center %s is %s",
                  data_center.name, data_center.status)
        if data_center.status == types.DataCenterStatus.UP:
            return

        log.info("Data center %s is %s, waiting until it is up",
                 data_center.name, data_center.status)

        while True:
            time.sleep(self.conf["poll_interval"])
            data_center = data_center_service.get()
            log.debug("Data center %s is %s",
                      data_center.name, data_center.status)
            if data_center.status == types.DataCenterStatus.UP:
                break

            if time.monotonic() > deadline:
                raise Timeout(
                    "Timeout waiting until data center {} is up"
                    .format(data_center.name))

        log.info("Data center %s recovered in %d seconds",
                 data_center.name, time.monotonic() - start)

    # Modifying VMs.

    def create_vm(self):
        vm_name = "{}-{}-{}".format(
            self.conf["vm_name"], self.index, self.iteration)
        log.info("Creating vm %s", vm_name)

        start = time.monotonic()
        deadline = start + self.conf["create_vm_timeout"]

        vms_service = self.connection.system_service().vms_service()
        correlation_id = str(uuid.uuid4())

        self.vm = vms_service.add(
            types.Vm(
                name=vm_name,
                cluster=types.Cluster(name=self.conf["cluster_name"]),
                template=types.Template(name=self.conf["template_name"]),
                placement_policy=types.VmPlacementPolicy(
                    hosts=[
                        types.Host(name=self.conf["vm_host"])
                    ]
                ),
            ),
            # Clone to VM to keep raw disks raw.
            clone=True,
            query={'correlation_id': correlation_id},
        )

        try:
            self.wait_for_jobs(correlation_id, deadline)
        except JobError:
            self.vm = None
            raise

        log.info("VM %s created in %d seconds",
                 self.vm.name, time.monotonic() - start)

    def start_vm(self):
        log.info("Starting vm %s", self.vm.name)

        start = time.monotonic()
        deadline = start + self.conf["start_vm_timeout"]

        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)
        vm_service.start()

        self.wait_for_vm_status(types.VmStatus.UP, deadline)

        log.info("VM %s started in %d seconds",
                 self.vm.name, time.monotonic() - start)

    def stop_vm(self):
        log.info("Stopping vm %s", self.vm.name)

        start = time.monotonic()
        deadline = start + self.conf["stop_vm_timeout"]

        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)

        vm = vm_service.get()

        if vm.status == types.VmStatus.IMAGE_LOCKED:
            self.wait_for_vm_status(types.VmStatus.DOWN, deadline)
        elif vm.status != types.VmStatus.DOWN:
            self.try_to_stop_vm(deadline)

        log.info("VM %s stopped in %d seconds",
                 self.vm.name, time.monotonic() - start)

    def try_to_stop_vm(self, deadline):
        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)

        # Testing shows that if a vm is in WAIT_FOR_LUNCH state,
        # stopping it does nothing. To handle all possible cases, lets
        # repeat the stop request and check the status until the VM is
        # DOWN or the timeout expires.

        while True:
            try:
                vm_service.stop()
            except sdk.Error as e:
                log.warning("Error stopping vm %s: %s", self.vm.name, e)

            time.sleep(self.conf["poll_interval"])
            vm = vm_service.get()
            log.debug("VM %s is %s", self.vm.name, vm.status)
            if vm.status == types.VmStatus.DOWN:
                break

            if time.monotonic() > deadline:
                raise Timeout(
                    "Timeout stopping vm {}".format(self.vm.name))

    def remove_vm(self):
        log.info("Removing vm %s", self.vm.name)

        start = time.monotonic()
        deadline = start + self.conf["remove_vm_timeout"]

        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)

        vm_service.remove()

        while True:
            if time.monotonic() > deadline:
                raise Timeout("Timeout removing vm {}".format(self.vm.name))

            time.sleep(self.conf["poll_interval"])
            try:
                vm = vm_service.get()
            except sdk.NotFoundError:
                break
            except sdk.Error as e:
                log.warning("Error polling vm %s status, retrying: %s",
                            self.vm.name, e)
                continue

            log.debug("VM %s status: %s", self.vm.name, vm.status)

        log.info("VM %s removed in %d seconds",
                 self.vm.name, time.monotonic() - start)
        self.vm = None

    def wait_for_vm_status(self, status, deadline):
        log.info("Waiting until vm %s is %s", self.vm.name, status)

        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)

        while True:
            if time.monotonic() > deadline:
                raise Timeout(
                    "Timeout waiting until vm {} is {}"
                    .format(self.vm.name, status))

            time.sleep(self.conf["poll_interval"])
            try:
                vm = vm_service.get()
            except sdk.NotFoundError:
                # Adding vm failed.
                self.vm = None
                raise
            except sdk.Error as e:
                log.warning("Error polling vm, retrying: %s", e)
                continue

            if vm.status == status:
                break

            log.debug("VM %s status: %s", self.vm.name, vm.status)

    # Modifying snapshots.

    def create_snapshot(self):
        log.info("Creating snapshot for vm %s", self.vm.name)

        start = time.monotonic()
        deadline = start + self.conf["create_snapshot_timeout"]

        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)
        snapshots_service = vm_service.snapshots_service()
        correlation_id = str(uuid.uuid4())

        self.snapshot = snapshots_service.add(
            types.Snapshot(
                description='Snapshot 1',
                persist_memorystate=False
            ),
            query={'correlation_id': correlation_id},
        )

        try:
            self.wait_for_jobs(correlation_id, deadline)
        except JobError:
            self.snapshot = None
            raise

        log.info("Snapshot %s for vm %s created in %d seconds",
                 self.snapshot.id, self.vm.name, time.monotonic() - start)

    def remove_snapshot(self):
        log.info("Removing snapshot %s for vm %s",
                 self.snapshot.id, self.vm.name)

        start = time.monotonic()
        deadline = start + self.conf["remove_snapshot_timeout"]

        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)
        snapshots_service = vm_service.snapshots_service()
        snapshot_service = snapshots_service.snapshot_service(self.snapshot.id)
        correlation_id = str(uuid.uuid4())

        snapshot_service.remove(query={'correlation_id': correlation_id})

        self.wait_for_jobs(correlation_id, deadline)

        log.info("Snapshot %s for vm %s removed in %d seconds",
                 self.snapshot.id, self.vm.name, time.monotonic() - start)
        self.snapshot = None

    # Accessing guest.

    def write_data(self):
        try:
            vm_address = self.find_vm_address()
        except Timeout:
            log.warning("Timeout finding vm %s address, skipping write data",
                        self.vm.name)
            return

        log.info("Writing data in vm %s address %s", self.vm.name, vm_address)
        start = time.monotonic()

        script = b"""
            for disk in /dev/vd*; do
                dd if=/dev/zero bs=1M count=2048 \
                   of=$disk oflag=direct conv=fsync &
            done
            wait
        """

        cmd = [
            "ssh",
            # Disable host key verification, we trust the vm we just created.
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=no",
            "-l", "root",
            vm_address,
            "bash", "-s",
        ]

        log.debug("Running command: %s", cmd)

        r = subprocess.run(
            cmd,
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

        if r.returncode != 0:
            log.warning("Command failed rc=%r out=%r err=%r",
                        r.returncode, r.stdout, r.stderr)
        else:
            log.debug("Command succeeded out=%r err=%r",
                      r.stdout, r.stderr)

        log.info("Writing data completed in %d seconds",
                 time.monotonic() - start)

    def find_vm_address(self):
        start = time.monotonic()
        deadline = start + self.conf["vm_address_timeout"]

        vms_service = self.connection.system_service().vms_service()
        vm_service = vms_service.vm_service(self.vm.id)
        nics_service = vm_service.nics_service()

        while True:
            if time.monotonic() > deadline:
                raise Timeout(
                    "Timeout waiting for vm {} address".format(self.vm.name))

            for nic in nics_service.list():
                nic_service = nics_service.nic_service(nic.id)
                devices_service = nic_service.reported_devices_service()
                for device in devices_service.list():
                    device_service = devices_service.reported_device_service(device.id)
                    device = device_service.get()
                    # Sometiems device.ips is None.
                    if device.ips:
                        for ip in device.ips:
                            if ip.version == types.IpVersion.V4:
                                log.info(
                                    "Found vm %s address in %d seconds",
                                    self.vm.name, time.monotonic() - start)
                                return ip.address

            time.sleep(self.conf["poll_interval"])

    # Polling jobs.

    def wait_for_jobs(self, correlation_id, deadline):
        log.info("Waiting for jobs with correlation id %s",
                 correlation_id)

        while not self.jobs_completed(correlation_id):
            time.sleep(self.conf["poll_interval"])
            if time.monotonic() > deadline:
                raise Timeout(
                    "Timeout waiting for jobs with correlation id {}"
                    .format(correlation_id))

    def jobs_completed(self, correlation_id):
        """
        Return True if all jobs with specified correlation id have completed,
        False otherwise.

        Raise JobError if some jobs have failed or aborted.
        """
        jobs_service = self.connection.system_service().jobs_service()

        try:
            jobs = jobs_service.list(
                search="correlation_id={}".format(correlation_id))
        except sdk.Error as e:
            log.warning(
                "Error searching for jobs with correlation id %s: %s",
                correlation_id, e)
            # We dont know, assume that jobs did not complete yet.
            return False

        if all(job.status != types.JobStatus.STARTED for job in jobs):
            # In some cases like create snapshot, it is not be possible to
            # detect the failure by checking the entity.
            failed_jobs = [(job.description, str(job.status))
                           for job in jobs
                           if job.status != types.JobStatus.FINISHED]
            if failed_jobs:
                raise JobError(
                    "Some jobs for with correlation id {} have failed: {}"
                    .format(correlation_id, failed_jobs))

            return True
        else:
            jobs_status = [(job.description, str(job.status)) for job in jobs]
            log.debug("Some jobs with correlation id %s are running: %s",
                      correlation_id, jobs_status)
            return False


with open("conf.yml") as f:
    conf = yaml.safe_load(f)

logging.basicConfig(
    level=logging.DEBUG if conf["debug"] else logging.INFO,
    format="%(asctime)s %(levelname)-7s (%(threadName)s) %(message)s")

start = time.monotonic()
stats = Counter()
runners = []

for i in range(conf["vms_count"]):
    name = "run/{}".format(i)
    log.info("Starting runner %s", name)
    r = Runner(i, conf)
    t = threading.Thread(target=r.run, name=name, daemon=True)
    t.start()
    runners.append((r, t))

    log.info("Waiting %d seconds before starting next runner", conf["run_delay"])
    time.sleep(conf["run_delay"])

for r, t in runners:
    log.info("Waiting for runner %s", t.name)
    t.join()
    stats["passed"] += r.passed
    stats["failed"] += r.failed
    stats["errored"] += r.errored

log.info("%d failed, %d passed, %d errored in %d seconds",
         stats["failed"], stats["passed"], stats["errored"],
         time.monotonic() - start)
