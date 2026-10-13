import json
import tempfile
import unittest
from pathlib import Path
from monitor import Connection,Monitor,HOST_REVISION


class FakeSocket:
    def __init__(self,chunks):self.chunks=iter(chunks)
    def recv(self,count):return next(self.chunks)
    def sendall(self,data):pass


class StreamEvidenceTests(unittest.TestCase):
    def connection(self,chunks):
        connection=Connection.__new__(Connection)
        connection.tcp=True;connection.io=FakeSocket(chunks)
        connection.pending=b'';connection.capture_rx_fault=None
        return connection

    def test_split_invalid_line_preserves_original_bytes_and_still_rejects(self):
        connection=self.connection([b'D,1,\xff',b'\xf0\r\nACK,STOP\n'])
        with tempfile.TemporaryDirectory() as td:
            m=Monitor(connection,Path(td)/'session',three=True)
            try:
                m.hello=True
                self.assertEqual(connection.read(),[])
                lines=connection.read()
                self.assertEqual(lines[1],'ACK,STOP')
                with self.assertRaisesRegex(ConnectionError,'Invalid bytes'):m.receive(lines[0])
                entries=[json.loads(x) for x in Path(m.trace.name).read_text().splitlines()]
                raw=next(x for x in entries if x['kind']=='RX_FAULT_BYTES')
                self.assertEqual(bytes.fromhex(raw['hex']),b'D,1,\xff\xf0\r\n')
                self.assertEqual(raw['byte_count'],8)
                self.assertEqual(entries[0]['host_revision'],HOST_REVISION)
            finally:
                for f in (m.wire,m.trace,m.raw):f.close()

    def test_normal_ascii_does_not_create_extra_fault_records(self):
        connection=self.connection([b'ACK,STOP\r\n']);events=[]
        connection.capture_rx_fault=lambda data,reason:events.append((data,reason))
        self.assertEqual(connection.read(),['ACK,STOP']);self.assertEqual(events,[])

    def test_overflow_is_preserved_before_rejection(self):
        connection=self.connection([b'x'*65537]);events=[]
        connection.capture_rx_fault=lambda data,reason:events.append((data,reason))
        with self.assertRaisesRegex(ConnectionError,'framing overflow'):connection.read()
        self.assertEqual(events,[(b'x'*65537,'framing_overflow')])

if __name__=='__main__':unittest.main()
