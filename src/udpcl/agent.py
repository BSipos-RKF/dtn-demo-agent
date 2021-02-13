'''
Implementation of a symmetric UDPCL agent.
'''
import copy
from dataclasses import dataclass
import enum
from typing import Optional, BinaryIO
import ipaddress
import logging
import os
from io import BytesIO
import socket
import struct
import cbor2
import portion
import dbus.service
from gi.repository import GLib as glib
import dtls


def addr_family(ipaddr):
    if isinstance(ipaddr, ipaddress.IPv4Address):
        return socket.AF_INET
    elif isinstance(ipaddr, ipaddress.IPv6Address):
        return socket.AF_INET6
    else:
        raise ValueError('Not an IP address')


@enum.unique
class ExtensionKey(enum.IntFlag):
    ''' Extension map keys.
    '''
    NODEID = 0x01
    TRANSFER = 0x02
    STARTTLS = 0x05


@dataclass
class BundleItem(object):
    ''' State for RX and TX full bundles.
    '''

    #: The remote address
    address: str
    #: The remote port
    port: int
    #: Binary file to store in
    file: BinaryIO
    #: The unique transfer ID number.
    transfer_id: Optional[int] = None
    #: Size of the bundle data
    total_length: Optional[int] = None


@dataclass
class Transfer(object):
    ''' State for fragmented transfers.
    '''

    #: The remote address
    address: str
    #: The remote port
    port: int
    #: Transfer ID
    xfer_id: int
    #: Total transfer size
    total_length: int
    # Range of full data expected
    total_valid: Optional[portion.Interval] = None
    #: Range of real data present
    valid: Optional[portion.Interval] = None
    #: Accumulated byte string
    data: Optional[bytearray] = None

    @property
    def key(self):
        return tuple((self.address, self.port, self.xfer_id))

    def validate(self, other):
        ''' Validate an other transfer against this base.
        '''
        if other.total_length != self.total_length:
            raise ValueError('Mismatched total length')


def ssl_config_ctx(ctx):
    pass


class Agent(dbus.service.Object):
    ''' Overall agent behavior.

    :param config: The agent configuration object.
    :type config: :py:class:`Config`
    :param bus_kwargs: Arguments to :py:class:`dbus.service.Object` constructor.
        If not provided the default dbus configuration is used.
    :type bus_kwargs: dict or None
    '''

    DBUS_IFACE = 'org.ietf.dtn.udpcl.Agent'

    def __init__(self, config, bus_kwargs=None):
        self.__logger = logging.getLogger(self.__class__.__name__)
        self._config = config
        self._on_stop = None

        self._bindsocks = {}
        #: Map from socket to glib io-watch ID
        self._listen_plain = {}
        # Existing DTLS sessions, map from addr tuple to `dtls.SSLConnection`
        self._dtls_sess = {}

        self._tx_id = 0
        self._tx_queue = []
        #: map from transfer ID to :py:cls:`Transfer`
        self._rx_fragments = {}
        self._rx_id = 0
        self._rx_queue = {}

        if bus_kwargs is None:
            bus_kwargs = dict(
                conn=config.bus_conn,
                object_path='/org/ietf/dtn/udpcl/Agent'
            )
        dbus.service.Object.__init__(self, **bus_kwargs)

        if self._config.bus_service:
            self._bus_serv = dbus.service.BusName(
                bus=self._config.bus_conn,
                name=self._config.bus_service,
                do_not_queue=True
            )
            self.__logger.info('Registered as "%s"', self._bus_serv.get_name())

        for item in self._config.init_listen:
            self.listen(item.address, item.port, item.opts)

    def set_on_stop(self, func):
        ''' Set a callback to be run when this agent is stopped.

        :param func: The callback, which takes no arguments.
        '''
        self._on_stop = func

    @dbus.service.method(DBUS_IFACE, in_signature='')
    def stop(self):
        ''' Immediately stop the agent and disconnect any sessions. '''
        self.__logger.info('Stopping agent')
        for spec in tuple(self._bindsocks.keys()):
            self.listen_stop(*spec)

        if self._on_stop:
            self._on_stop()

    def exec_loop(self):
        ''' Run this agent in an event loop.
        The on_stop callback is replaced to quit the event loop.
        '''
        eloop = glib.MainLoop()
        self.set_on_stop(eloop.quit)
        self.__logger.info('Starting event loop')
        try:
            eloop.run()
        except KeyboardInterrupt:
            self.stop()

    @dbus.service.method(DBUS_IFACE, in_signature='sia{sv}')
    def listen(self, address, port, opts=None):
        ''' Begin listening for incoming transfers and defer handling
        connections to `glib` event loop.
        '''
        if opts is None:
            opts = {}

        bindspec = (address, port)
        if bindspec in self._bindsocks:
            raise dbus.DBusException('Already listening')

        ipaddr = ipaddress.ip_address(address)
        sock = socket.socket(addr_family(ipaddr), socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.__logger.info('Listening on %s:%d', address or '*', port)
        sock.bind(bindspec)

        multicast_member = opts.get('multicast_member', [])
        for item in multicast_member:
            addr = str(item['addr'])

            if isinstance(ipaddr, ipaddress.IPv4Address):
                self.__logger.info('Listening for multicast %s', addr)
                mreq = struct.pack("=4sl", socket.inet_aton(addr), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            elif isinstance(ipaddr, ipaddress.IPv6Address):
                iface = item['iface']
                iface_ix = socket.if_nametoindex(iface)
                self.__logger.info('Listening for multicast %s on %s (%s)', addr, iface, iface_ix)
                mreq = (
                    socket.inet_pton(socket.AF_INET6, addr)
                    +struct.pack("@I", iface_ix)
                )
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)

        self._bindsocks[bindspec] = sock
        self._listen_plain[sock] = glib.io_add_watch(sock, glib.IO_IN, self._sock_recvfrom)

    @dbus.service.method(DBUS_IFACE, in_signature='si')
    def listen_stop(self, address, port):
        ''' Stop listening for transfers on an existing port binding.
        '''
        bindspec = (address, port)
        if bindspec not in self._bindsocks:
            raise dbus.DBusException('Not listening')

        sock = self._bindsocks.pop(bindspec)
        self.__logger.info('Un-listening on %s:%d', address or '*', port)
        if sock in self._listen_plain:
            glib.source_remove(self._listen_plain.pop(sock))
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except socket.error as err:
            self.__logger.warning('Bind socket shutdown error: %s', err)
        sock.close()

    def _sock_recvfrom(self, sock, *_args, **_kwargs):
        ''' Callback to handle incoming datagrams.

        :return: True to continue listening.
        '''
        self.__logger.info('Plain recv')
        data, fromaddr = sock.recvfrom(64 * 1024)

        address = fromaddr[0]
        port = fromaddr[1]
        self.__logger.info('Received %d octets from %s:%d',
                           len(data), address, port)
        self._recv_datagram(sock, data, address, port)
        return True

    def _starttls(self, sock, addr: tuple, server_side: bool):
        # ignore plaintext during and after handshake
        if sock in self._listen_plain:
            glib.source_remove(self._listen_plain.pop(sock))

        conn = dtls.SSLConnection(
            sock,
            do_handshake_on_connect=False,
            server_side=server_side,
            ciphers=self._config.dtls_ciphers,
            ca_certs=self._config.dtls_ca_file,
            keyfile=self._config.dtls_key_file,
            certfile=self._config.dtls_cert_file,
            cert_reqs=dtls.sslconnection.CERT_REQUIRED,
            cb_user_config_ssl_ctx=ssl_config_ctx,
        )
        conn._intf_ssl_ctx.set_ssl_logging(True)
        if server_side:
            conn.listen()
        else:
            # as client
            conn.connect(addr)

        self.__logger.info('Starting TLS handshake on %s', sock)
        conn.do_handshake()
        # after establishment
        self._dtls_sess[addr] = conn

        glib.io_add_watch(
            conn.get_socket(inbound=True),
            glib.IO_IN,
            self._dtlsconn_recv,
            conn, addr
        )

        return conn

    def _dtlsconn_recv(self, _src, _cond, conn, addr, *_args, **_kwargs):
        self.__logger.info('DTLS recv')
        data = conn.read(64 * 1024)
        self.__logger.info('Received %d octets from DTLS with %s',
                           len(data), addr)
        self._recv_datagram(None, data, addr[0], addr[1])
        return True

    def _recv_datagram(self, sock, data, address, port):
        DTLS_FIRST_OCTETS = (
            20,  # change_cipher_spec
            21,  # alert
            22,  # handshake
            23,  # application_data
        )

        first_octet = data[0]
        major_type = first_octet >> 5
        if first_octet == 0x00:
            self.__logger.info('Ignoring padding data')

        elif first_octet in DTLS_FIRST_OCTETS:
            self.__logger.error('Unexpected DTLS handshake')
            if sock:
                self._starttls(sock, (address, port), server_side=True)
            else:
                self.__logger.error('Ignored DTLS message *within* DTLS plaintext')

        elif first_octet == 0x06:
            self.__logger.error('Ignoring BPv6 bundle')

        elif major_type == 4:
            self._add_rx_item(
                BundleItem(
                    address=address,
                    port=port,
                    total_length=len(data),
                    file=BytesIO(data)
                )
            )

        elif major_type == 5:
            # Map type
            extmap = cbor2.loads(data)
            if ExtensionKey.STARTTLS in extmap:
                if sock:
                    self._starttls(sock, (address, port), server_side=True)
                else:
                    self.__logger.error('Ignored STARTTLS *within* DTLS plaintext')

            if ExtensionKey.TRANSFER in extmap:
                xfer_id, total_len, frag_offset, frag_data = extmap[ExtensionKey.TRANSFER]
                new_xfer = Transfer(
                    address=address,
                    port=port,
                    xfer_id=xfer_id,
                    total_length=total_len,
                )

                xfer = self._rx_fragments.get(new_xfer.key)
                if xfer:
                    xfer.validate(new_xfer)
                else:
                    xfer = new_xfer
                    self._rx_fragments[xfer.key] = xfer
                    xfer.total_valid = portion.closedopen(0, xfer.total_length)
                    xfer.valid = portion.empty()
                    xfer.data = bytearray(xfer.total_length)

                self.__logger.debug('Handling transfer %d fragment offset %d size %d', xfer.xfer_id, frag_offset, len(frag_data))
                end_ix = frag_offset + len(frag_data)
                xfer.data[frag_offset:end_ix] = frag_data

                xfer.valid |= portion.closedopen(frag_offset, end_ix)
                if xfer.valid == xfer.total_valid:
                    self.__logger.info('Finished transfer %d size %d', xfer.xfer_id, xfer.total_length)
                    del self._rx_fragments[xfer.key]
                    self._add_rx_item(
                        BundleItem(
                            address=xfer.address,
                            port=xfer.port,
                            total_length=xfer.total_length,
                            file=BytesIO(xfer.data)
                        )
                    )

        else:
            self.__logger.warn('Ignoring unknown datagram type')

    def _add_rx_item(self, item):
        if item.transfer_id is None:
            item.transfer_id = copy.copy(self._rx_id)
            self._rx_id += 1

        self._rx_queue[item.transfer_id] = item
        self.recv_bundle_finished(str(item.transfer_id), item.total_length)

    @dbus.service.signal(DBUS_IFACE, signature='st')
    def recv_bundle_finished(self, bid, length):
        pass

    @dbus.service.method(DBUS_IFACE, in_signature='', out_signature='as')
    def recv_bundle_get_queue(self):
        return dbus.Array([str(bid) for bid in self._rx_queue.keys()])

    @dbus.service.method(DBUS_IFACE, in_signature='s', out_signature='ay')
    def recv_bundle_pop_data(self, bid):
        bid = int(bid)
        item = self._rx_queue.pop(bid)
        item.file.seek(0)
        return item.file.read()

    @dbus.service.method(DBUS_IFACE, in_signature='ss', out_signature='')
    def recv_bundle_pop_file(self, bid, filepath):
        bid = int(bid)
        item = self._rx_queue.pop(bid)
        item.file.seek(0)

        import shutil
        out_file = open(filepath, 'wb')
        shutil.copyfileobj(item.file, out_file)

    @dbus.service.method(DBUS_IFACE, in_signature='siay', out_signature='s')
    def send_bundle_data(self, address, port, data):
        ''' Send bundle data directly.
        '''
        # byte array to bytes
        data = b''.join([bytes([val]) for val in data])

        item = BundleItem(
            address=str(address),
            port=int(port),
            file=BytesIO(data)
        )
        return str(self._add_tx_item(item))

    @dbus.service.method(DBUS_IFACE, in_signature='sis', out_signature='s')
    def send_bundle_file(self, address, port, filepath):
        ''' Send a bundle from the filesystem.
        '''
        item = BundleItem(
            address=str(address),
            port=int(port),
            file=open(filepath, 'rb')
        )
        return str(self._add_tx_item(item))

    def _add_tx_item(self, item):
        if item.transfer_id is None:
            item.transfer_id = copy.copy(self._tx_id)
            self._tx_id += 1

        item.file.seek(0, os.SEEK_END)
        item.total_length = item.file.tell()
        item.file.seek(0)

        self._tx_queue.append(item)

        self._process_tx_queue_trigger()
        return item.transfer_id

    def _process_tx_queue_trigger(self):
        if self._tx_queue:
            glib.idle_add(self._process_tx_queue)

    def _send_transfer(self, sender, item):
        ''' Send a transfer, fragmenting if necessary.

        :param sender: A datagram sending function.
        :param item: The item to send.
        :type item: :py:cls:`BundleItem`
        '''
        mtu = self._config.mtu_default
        data = item.file.read()

        segments = []
        self.__logger.info('Transfer %d size %d relative to MTU %s',
                           item.transfer_id, len(data), mtu)
        if mtu is None or len(data) < mtu:
            segments = [data]
        else:
            # The base extension map with the largest values present
            ext_base = {
                ExtensionKey.TRANSFER: [
                    item.transfer_id,
                    item.total_length,
                    item.total_length,
                    b'',
                ],
            }
            ext_base_encsize = len(cbor2.dumps(ext_base))
            # Largest bstr head size
            data_size_encsize = len(cbor2.dumps(item.total_length))
            # Size left for fragment data
            remain_size = mtu - (ext_base_encsize - 1 + data_size_encsize)

            frag_offset = 0
            while frag_offset < len(data):
                ext = {
                    ExtensionKey.TRANSFER: [
                        item.transfer_id,
                        item.total_length,
                        frag_offset,
                        data[frag_offset:(frag_offset + remain_size)],
                    ],
                }
                frag_offset += remain_size
                segments.append(cbor2.dumps(ext))

        for seg in segments:
            self.__logger.debug('Sending datagram size %d', len(seg))
            sender(seg)

    def _process_tx_queue(self):
        ''' Perform the next TX bundle if possible.

        :return: True to continue processing at a later time.
        :rtype: bool
        '''
        if not self._tx_queue:
            return
        self.__logger.debug('Processing queue of %d items',
                            len(self._tx_queue))

        # work from the head of the list
        item = self._tx_queue.pop(0)

        self.send_bundle_started(
            str(item.transfer_id),
            item.total_length
        )

        ipaddr = ipaddress.ip_address(item.address)
        is_ipv4 = isinstance(ipaddr, ipaddress.IPv4Address)
        is_ipv6 = isinstance(ipaddr, ipaddress.IPv6Address)
        self.__logger.info('Sending %d octets to %s:%d',
                           item.total_length, item.address, item.port)
        sock = socket.socket(addr_family(ipaddr), socket.SOCK_DGRAM, socket.IPPROTO_UDP)

        if self._config.tx_port:
            self.__logger.debug('Sending from fixed port %d', self._config.tx_port)
            anyaddress = '127.0.0.2' if is_ipv4 else '::'
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((anyaddress, self._config.tx_port))

        if is_ipv4:
            addr = (item.address, item.port)
        else:
            addr = (item.address, item.port, 0, 0)

        def simplesender(data):
            ''' Send to a single destination
            '''
            sock.sendto(data, addr)

        if ipaddr.is_multicast:
            multicast = self._config.multicast

            loop = 1
            if loop is not None:
                if is_ipv4:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, loop)
                else:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, loop)

            if multicast.ttl is not None:
                if is_ipv4:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast.ttl)
                else:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, multicast.ttl)

            # if provided, iterate over different source interfaces
            if is_ipv4 and multicast.v4sources:
                for src in multicast.v4sources:
                    self.__logger.debug('Using multicast %s', src)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(src))
                    self._send_transfer(simplesender, item)
            elif is_ipv6 and multicast.v6sources:
                for src in multicast.v6sources:
                    iface_ix = socket.if_nametoindex(src)
                    self.__logger.debug('Using multicast %s (%s)', src, iface_ix)
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, iface_ix)
                    self._send_transfer(simplesender, item)
            else:
                self._send_transfer(simplesender, item)

        else:
            # unicast transfer

            if self._config.dtls_enable_tx:
                conn = self._dtls_sess.get(addr)
                if conn:
                    self.__logger.debug('Using existing session with %s', addr)
                else:
                    self.__logger.debug('Need new session with %s', addr)
                    sock.sendto(cbor2.dumps({ExtensionKey.STARTTLS: None}), addr)
                    conn = self._starttls(sock, addr, server_side=False)
                sender = conn.write
            else:
                sender = simplesender

            self._send_transfer(sender, item)

        self.send_bundle_finished(
            str(item.transfer_id),
            item.total_length,
            'success'
        )

        return bool(self._tx_queue)

    @dbus.service.signal(DBUS_IFACE, signature='st')
    def send_bundle_started(self, bid, length):
        pass

    @dbus.service.signal(DBUS_IFACE, signature='sts')
    def send_bundle_finished(self, bid, length, result):
        pass

