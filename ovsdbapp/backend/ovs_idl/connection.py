# Copyright (c) 2015 Red Hat, Inc.
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

import os
import threading
import traceback

from ovs.db import idl
from ovs import poller
from six.moves import queue as Queue

from ovsdbapp.backend.ovs_idl import idlutils

if os.name == 'nt':
    from ovsdbapp.backend.ovs_idl.windows import connection_utils
else:
    from ovsdbapp.backend.ovs_idl.linux import connection_utils


class TransactionQueue(Queue.Queue, object):
    def __init__(self, *args, **kwargs):
        super(TransactionQueue, self).__init__(*args, **kwargs)
        self._wait_queue = connection_utils.WaitQueue(
            max_queue_size=self.maxsize)

    def get_nowait(self, *args, **kwargs):
        try:
            result = super(TransactionQueue, self).get_nowait(*args, **kwargs)
        except Queue.Empty:
            return None
        self._wait_queue.alert_notification_consume()
        return result

    def put(self, *args, **kwargs):
        super(TransactionQueue, self).put(*args, **kwargs)
        self._wait_queue.alert_notify()

    @property
    def alert_fileno(self):
        return self._wait_queue.alert_fileno


class Connection(object):

    def __init__(self, idl, timeout):
        """Create a connection to an OVSDB server using the OVS IDL

        :param timeout: The timeout value for OVSDB operations
        :param idl: A newly created ovs.db.Idl instance (run never called)
        """
        self.timeout = timeout
        self.txns = TransactionQueue(1)
        self.lock = threading.Lock()
        self.idl = idl
        self.thread = None

    def start(self):
        """Start the connection."""
        with self.lock:
            if self.thread is not None:
                return False
            if not self.idl.has_ever_connected():
                idlutils.wait_for_change(self.idl, self.timeout)
                try:
                    self.idl.post_connect()
                except AttributeError:
                    # An ovs.db.Idl class has no post_connect
                    pass
            self.poller = poller.Poller()
            self.thread = threading.Thread(target=self.run)
            self.thread.setDaemon(True)
            self.thread.start()

    def run(self):
        while True:
            self.idl.wait(self.poller)
            self.poller.fd_wait(self.txns.alert_fileno, poller.POLLIN)
            # TODO(jlibosva): Remove next line once losing connection to ovsdb
            #                 is solved.
            self.poller.timer_wait(self.timeout * 1000)
            self.poller.block()
            self.idl.run()
            txn = self.txns.get_nowait()
            if txn is not None:
                try:
                    txn.results.put(txn.do_commit())
                except Exception as ex:
                    er = idlutils.ExceptionResult(ex=ex,
                                                  tb=traceback.format_exc())
                    txn.results.put(er)
                self.txns.task_done()

    def queue_txn(self, txn):
        self.txns.put(txn)


class OvsdbIdl(idl.Idl):
    @classmethod
    def from_server(cls, connection_string, schema_name):
        """Create the Idl instance by pulling the schema from OVSDB server"""
        helper = idlutils.get_schema_helper(connection_string, schema_name)
        helper.register_all()
        return cls(connection_string, helper)

    def post_connect(self):
        """Operations to execute after the Idl has connected to the server

        An example would be to set up Idl notification handling for watching
        and unwatching certain OVSDB change events
        """