# -*- coding: utf-8 -*-
""" libAvrCom
    A light communication framework over serial


    F. CLVZ - October 2014
"""
from __future__ import print_function
from collections import deque
from Queue import Queue
import threading
import logging
import serial
import struct
import time


class Constants:
    """ Constants in use in this module """
    PARSER_IDLE = 0
    PARSER_HEADER_RECEIVED = 1
    COMM_HEADER_1 = 0x41  # 'A'
    COMM_HEADER_2 = 0x67  # 'g'
    # Callback types
    CBACK_Permanent = 0
    CBACK_OneShot = 1


class AvrComMaster(object):
    """ Master object for AvrCom Framework """
    def __init__(self, port='/dev/ttyUSB0', baudrate=57600):
        self.log = logging.getLogger(__name__+".AvrComMaster")

        # Rx data parser initialization
        self._parser_status = Constants.PARSER_IDLE

        # Commands and callback mechanism
        self._command_queue = deque()
        self._cmd_callbacks = {}

        # serial port initialization

        # we use a timeout to make sure we can exit the RxThread nicelly
        self._serial = serial.Serial(port, baudrate, timeout=0.5)
        self._ThreadsStopFlag = threading.Event()

        self._rxqueue = deque()
        self._rx_thread = RxThread(serial_port=self._serial,
                                   data_queue=self._rxqueue,
                                   stop_event=self._ThreadsStopFlag)

        self._txqueue = Queue()
        # No need to start the thread now, there is no data to send yet.
        self._restart_tx_thread(start=False)

    def __del__(self):
        """ Do a clean stop of the threads """
        self._ThreadsStopFlag.set()
        self._rx_thread.join()

    def update(self):
        """
        this is the class's main function. It should be called periodically in
        the app's event loop. This function will handle the commands decoding
        and the callbacks calling.
        :return: None
        """
        self._rx_data_parser()
        self._cmd_callbacks_parser()

    def stop(self):
        """ Stop the data receiving and sending threads """
        self._ThreadsStopFlag.set()
        self._rx_thread.join()
        self._tx_thread.join()

    def start(self):
        """ Start the data receiving and sending threads """
        self._ThreadsStopFlag.clear()
        self._rx_thread.start()
        self._tx_thread.start()

    def register_callback(self, cmd_id, callback, cbk_type):
        """ Register a function callback for a given command id.
        Callback can be either *OneShot* (will only be called once, and then
        forgotten), or *Permanent* (will be called systematically after a
        command is received by the RxThread).

        Arguments:
          cmd_id: command's ID. type: integer
          callback: The function that will be called when the command is
                    received. Must be a callable object. It's first arg is a
                    Command() object.
          cbk_type: The callback type. Can be OneShot (Constants.CBACK_OneShot),
                    or permanent (Constants.CBACK_Permanent).
        """
        # The command ID must be an integer
        if type(cmd_id) is not int:
            raise TypeError

        # make sure that there is already an entry in the dict for this cmd id;
        # if not, initialize it
        if cmd_id not in self._cmd_callbacks:
            self.log.debug("Initializing callbacks dictionary \
                for command id={}".format(cmd_id))

            self._cmd_callbacks[cmd_id] = {
                Constants.CBACK_OneShot: [],
                Constants.CBACK_Permanent: []
            }

        cb_dict = self._cmd_callbacks[cmd_id]

        # Check for double entries for permanent cbks
        if cbk_type == Constants.CBACK_Permanent:
            if callback in cb_dict[Constants.CBACK_Permanent]:
                self.log.warning("RegisterCallback:: Rejecting permanent callback \
                    registration. "
                                 "Callback already present in the list; cmd_id={},\
                                  callback={}".format(cmd_id, callback))
                return

        cb_dict[cbk_type].append(callback)

    def send_command(self, cmd):
        """ Send a command, with no callback
        :param cmd: Command() object
        :return: None
        """
        data = self._build_packet(cmd)
        for d in data:
            self._txqueue.put(d)
        if not self._tx_thread.is_alive():
            self._restart_tx_thread()

    def send_command_with_callback(self, cmd, callback):
        """ Send a command after registering a OneShot callback. """
        self.register_callback(cmd.id, callback, Constants.CBACK_OneShot)
        self.send_command(cmd)

    def _restart_tx_thread(self, start=True):
        """ Restart the Tx Thread (obvious!)
        The TX Thread is stopped when there is no more data to send (in order
            not to lock when shutting down, mainly). It has to be recreated
            everytime, as a thread cannot be run twice.

            Arguments:
            start: If True, the thread will immediatly start. Else, it will
                   have to be started manually elsewhere.
        """
        self.log.debug("Creating a new TxThread")
        self._tx_thread = TxThread(serial_port=self._serial,
                                   data_queue=self._txqueue,
                                   stop_event=self._ThreadsStopFlag)
        if start:
            self.log.debug("Starting the TxThread")
            self._tx_thread.start()

    def _build_packet(self, cmd):
        """ Builds a packet from a command.
        CRC do not need to be calculated, it is done internally
        :param cmd: Command() object
        :return: a bytearray to send to the UART
        """
        plSize = len(cmd.data)
        if plSize > 255:
            # The payload size is encoded on a byte.
            raise Exception("Command too long (too much data)")

        header = struct.pack('BBBB', Constants.COMM_HEADER_1,
                             Constants.COMM_HEADER_2,
                             cmd.id, plSize)
        data = ""
        # for d in cmd.data:
        #     data += struct.pack('B', d)
        # Equivalent, but maybe more optimized:
        data += struct.pack(len(cmd.data) * 'B', *cmd.data)

        crc = cmd.get_CRC()
        footer = struct.pack(len(crc) * 'B', *crc)

        return header+data+footer

    def _rx_data_parser(self):
        """
        parse the data in the Rx buffer and interprets it as commands.
        """
        # As long as there is enough data to make the shortest possible command
        while len(self._rxqueue) >= 4:
            if self._parser_status == Constants.PARSER_IDLE:
                # look for the beginning of a frame
                while len(self._rxqueue) >= 2:
                    if self._rxqueue.popleft() == Constants.COMM_HEADER_1:
                        if self._rxqueue.popleft() == Constants.COMM_HEADER_2:
                            self._parser_status = Constants.PARSER_HEADER_RECEIVED
                            # We have found the beginning of a frame!
                            break

            # a frame start sequence has been received. Make sure there is
            # enough data left in the buffer to make a command
            if self._parser_status == Constants.PARSER_HEADER_RECEIVED \
                    and len(self._rxqueue) >= 4:  # 4: cmd, plSz + crc
                payload = self._rxqueue[1]  # Item 0 is the command ID.
                if not len(self._rxqueue) >= 4 + payload:
                    return
                cmd = Command()
                cmd.id = self._rxqueue.popleft()
                payload = self._rxqueue.popleft()
                for i in range(payload):
                    cmd.data.append(self._rxqueue.popleft())

                # Here, the CRC is implemented on two bytes.
                # TODO? Maybe add a constant here...
                cmd.crc.append(self._rxqueue.popleft())
                cmd.crc.append(self._rxqueue.popleft())

                cmd.rx_timestamp = time.time()
                # append the command to the list, so it is treated later in the
                # event loop
                self._command_queue.append(cmd)
                self._parser_status = Constants.PARSER_IDLE

    def _cmd_callbacks_parser(self):
        """
        goes through the input command queue and execute the callbacks
        """

        while len(self._command_queue) > 0:
            cmd = self._command_queue.popleft()
            self.log.debug("cmd_callbacks_parser::Treating command {}".format(cmd.id))
            try:
                cb_dict = self._cmd_callbacks[cmd.id]
                didCbk = False
                # OneShot callbacks
                try:
                    cbks = cb_dict[Constants.CBACK_OneShot]  # we should get a list here
                    try:
                        cbks[0](cmd)  # do the actual callback calling
                        # We take only the first element of the list as a callback. If there are others,
                        # they will be called when another command is received.
                        didCbk = True
                        del cbks[0]  # remove it from the list, now that it has been done
                    except IndexError:
                        # we arrive here if the callbacks list is empty. Nothing to do...
                        pass
                except KeyError:
                    # No onetime callback. Do nothing...
                    pass

                # Permanent callbacks
                # TODO: maybe find a way to factorize the code with the onetime callbacks. Quick and dirty for now
                try:
                    cbks = cb_dict[Constants.CBACK_Permanent]  # we should get a list here
                    for cbk in cbks:
                        cbk(cmd)  # do the actual callback calling
                        didCbk = True
                except KeyError:
                    # No onetime callback. Do nothing...
                    pass
                if not didCbk:
                    self.log.warning("Received known command with no registered callback; id={}".format(cmd.id))
            except KeyError:
                # No callbacks found for that particular command
                self.log.warning("Received unknown command; id={}".format(cmd.id))


class RxThread(threading.Thread):
    """ Data reception thread. """
    def __init__(self, serial_port, data_queue, stop_event):
        """
        :rtype : None
        :param serial_port: a reference to the serial port
        :param data_queue: a dequeue object for exchanging the received data with the parent thread
        :param stop_event: a threading.Event object. When set, the thread eventually stops.
        """
        threading.Thread.__init__(self)
        self.log = logging.getLogger(__name__+".RxThread")
        self._serial = serial_port
        self._rxqueue = data_queue
        self._stop_event = stop_event

    def run(self):
        self._serial.flushInput()  # doesn't seem to work...
        time.sleep(0.5)
        while not self._stop_event.is_set():
            data = self._serial.read(1)  # blocking call. timeout set to 0.5s
            if len(data) == 1:
                data = struct.unpack('B', data)[0]
                self._rxqueue.append(data)
            # len(data) != 1 means that we've reached the timeout.
        self.log.info("AvrCom RxThread terminating nicely.")


class TxThread(threading.Thread):
    """ Data emission thread. Push data to the serial stack.
    When the queue is empty, the thread is destroyed. When new data
    needs to be emitted, a new thread is created.
    """
    def __init__(self, serial_port, data_queue, stop_event):
        """
        :rtype : None
        :param serial_port: a reference to the serial port
        :param data_queue: a Queue object for exchanging the received data with
                           the parent thread
        (must be a Queue object, or similar, with a blocking call to get() ).
        :param stop_event: a threading.Event object. When set, the thread
                           eventually stops.
        """
        threading.Thread.__init__(self)
        self.log = logging.getLogger(__name__+".TxThread")
        self._serial = serial_port
        self._txqueue = data_queue
        self._stop_event = stop_event

    def run(self):
        while not self._stop_event.is_set() and not self._txqueue.empty():
            self._serial.write(self._txqueue.get())
        self.log.debug("Stopping TxThread.")


class Command(object):
    """ Command object. Fully represents a command
    in the "framework".
    """
    def __init__(self):
        self.id = None
        self.data = []
        self.crc = []
        self._IsCrcComputed = False
        self.rx_timestamp = None

    def get_CRC(self):
        """ Returns the CRC for the command
        Compute it if it has not already been done.
        """
        if not self._IsCrcComputed:
            self.crc = self._compute_CRC()
            self._IsCrcComputed = True
        return self.crc

    def _compute_CRC(self):
        """ Compute the CRC of the command """
        # TODO
        return [0x42, 0x24]

    def __str__(self):
        """ Prints a nice representation of the command """
        str = "Command: {}\n\tdata: {}\n\tcrc: {}".format(self.id, self.data,
                                                          self.crc)
        return str
