''' Administrative endpoint.
'''
from base64 import urlsafe_b64encode, urlsafe_b64decode
import cbor2
from cryptography.hazmat.primitives import hashes
from dataclasses import dataclass, field, fields
import dbus
import enum
from gi.repository import GLib as glib
import logging
import math

from scapy_cbor.util import encode_diagnostic
from bp.encoding import (
    AbstractBlock, PrimaryBlock, CanonicalBlock,
)
from bp.util import BundleContainer, ChainStep
from bp.app.base import app, AbstractApplication

LOGGER = logging.getLogger(__name__)


@enum.unique
class RecordType(enum.IntEnum):
    STATUS = 1
    ACME = 99  # FIXME: not real allocation


@enum.unique
class AcmeKey(enum.IntEnum):
    TOKEN_PART1 = 1
    KEY_AUTH_HASH = 2


@dataclass
class AcmeChallenge(object):
    ''' Authorized ACME challenge data.
    '''

    #: The peer Node ID
    nodeid: str
    #: base64url encoded token
    token_part1_enc: str = None
    #: base64url encoded token
    token_part2_enc: str = None
    #: base64url encoded thumbprint
    key_tp_enc: str = None

    def key_auth_hash(self) -> bytes:
        ''' Compute the response digest.
        '''
        key_auth = (self.token_part1_enc + self.token_part2_enc + '.' + self.key_tp_enc)
        LOGGER.info('Key authorization string: %s', key_auth)
        digest = hashes.Hash(hashes.SHA256())
        digest.update(key_auth.encode('utf8'))
        return digest.finalize()

    @staticmethod
    def b64encode(data: bytes) -> str:
        enc = urlsafe_b64encode(data).rstrip(b'=')
        return enc.decode('latin1')

    @staticmethod
    def b64decode(enc: str) -> bytes:
        enc = enc.encode('latin1')
        enc = enc.ljust(int(math.ceil(len(enc) / 4)) * 4, b'=')
        return urlsafe_b64decode(enc)


@app('admin')
class Administrative(AbstractApplication):
    ''' Administrative element.
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._config = None
        self._rec_type_map = {
            RecordType.STATUS: self._recv_status,
            RecordType.ACME: self._recv_acme,
        }

        # ACME server map from (nodeid,token) to AcmeChallenge object
        self._acme_chal = {}
        # ACME client from (nodeid,token) to AcmeChallenge object
        self._acme_resp = {}

    def load_config(self, config):
        self._config = config

    def add_chains(self, rx_chain, tx_chain):
        rx_chain.append(ChainStep(
            order=-1,
            name='Administrative routing',
            action=self._rx_route
        ))
        rx_chain.append(ChainStep(
            order=30,
            name='Administrative handling',
            action=self._recv_bundle
        ))

    def _rx_route(self, ctr):
        eid = ctr.bundle.primary.destination
        if eid == self._config.node_id:
            ctr.record_action('deliver')

    def _recv_bundle(self, ctr):
        if not self._recv_for(ctr, self._config.node_id):
            return

        rec = cbor2.loads(ctr.block_num(1).getfieldval('btsd'))
        LOGGER.info('Record RX: %s', encode_diagnostic(rec))
        rec_type = int(rec[0])
        handler = self._rec_type_map[rec_type]

        handler(ctr, rec[1])

        return True

    def _recv_status(self, ctr, msg):
        pass

    def _recv_acme(self, ctr, msg):
        source = ctr.bundle.primary.source
        is_request = ctr.bundle.primary.bundle_flags & PrimaryBlock.Flag.USER_APP_ACK
        if is_request:
            try:
                chal = self._acme_resp[source]
            except KeyError:
                LOGGER.warning('Unexpected ACME request from %s', source)
                ctr.record_action('delete')
                return
            chal.token_part1_enc = AcmeChallenge.b64encode(msg[AcmeKey.TOKEN_PART1])

            msg = {
                AcmeKey.TOKEN_PART1: AcmeChallenge.b64decode(chal.token_part1_enc),
                AcmeKey.KEY_AUTH_HASH: chal.key_auth_hash(),
            }
            self.send_acme(ctr.bundle.primary.report_to, msg, False)

        else:
            try:
                chal = self._acme_chal[source]
            except KeyError:
                LOGGER.warning('Unexpected ACME response from %s', source)
                ctr.record_action('delete')
                return
            expect_auth_hash = chal.key_auth_hash()
            is_valid = msg[AcmeKey.KEY_AUTH_HASH] == expect_auth_hash

            self.got_acme_response(source, chal.token_part1_enc, is_valid)

    def send_acme(self, nodeid, msg, is_request):
        rec = [
            RecordType.ACME,
            msg
        ]

        pri_flags = PrimaryBlock.Flag.PAYLOAD_ADMIN
        if is_request:
            pri_flags |= (
                PrimaryBlock.Flag.REQ_DELETION_REPORT
                | PrimaryBlock.Flag.USER_APP_ACK
            )

        ctr = BundleContainer()
        ctr.bundle.primary = PrimaryBlock(
            bundle_flags=pri_flags,
            destination=str(nodeid),
            crc_type=AbstractBlock.CrcType.CRC32,
        )
        ctr.bundle.blocks = [
            CanonicalBlock(
                type_code=1,
                block_num=1,
                crc_type=AbstractBlock.CrcType.CRC32,
                btsd=cbor2.dumps(rec),
            ),
        ]
        self._agent.send_bundle(ctr)

    #: Interface name
    DBUS_IFACE = 'org.ietf.dtn.bp.admin'

    @dbus.service.method(DBUS_IFACE, in_signature='sss', out_signature='')
    def start_expect_acme_request(self, source, token_part2_enc, key_tp_enc):
        chal = AcmeChallenge(
            nodeid=source,
            token_part2_enc=token_part2_enc,
            key_tp_enc=key_tp_enc,
        )
        self._acme_resp[source] = chal

    @dbus.service.method(DBUS_IFACE, in_signature='ss', out_signature='')
    def stop_expect_acme_request(self, source, _token_part2_enc):
        del self._acme_resp[source]

    @dbus.service.method(DBUS_IFACE, in_signature='ssss', out_signature='')
    def send_acme_request(self, nodeid, token_part1_enc, token_part2_enc, key_tp_enc):
        chal = AcmeChallenge(
            nodeid=nodeid,
            token_part1_enc=token_part1_enc,
            token_part2_enc=token_part2_enc,
            key_tp_enc=key_tp_enc,
        )
        self._acme_chal[nodeid] = chal

        msg = {
            AcmeKey.TOKEN_PART1: AcmeChallenge.b64decode(token_part1_enc),
        }
        self.send_acme(nodeid, msg, True)

    @dbus.service.signal(DBUS_IFACE, signature='ssb')
    def got_acme_response(self, nodeid, token_part1_enc, is_valid):
        '''
        '''
