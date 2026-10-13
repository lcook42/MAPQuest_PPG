import unittest
from types import SimpleNamespace
from unittest.mock import patch
from diagnostic import parse,choose_connection

class DiagnosticTests(unittest.TestCase):
    def test_three_channels(self):
        self.assertEqual(parse('D,1,10,2,100,200,300,32,33,34')[3:6],[100,200,300])
        for line in ('S,1,10,2,100,200,32,32','D,1,10,2,100,200,300,32,33,0','D,1,10,2,100,200,300000,32,33,34'):
            with self.assertRaises(ValueError):parse(line)
    def test_usb_preferred(self):
        with patch('diagnostic.list_ports.comports',return_value=[SimpleNamespace(vid=0x10c4,pid=0xea60,device='COM4')]),patch('diagnostic.Connection') as c:
            _,label=choose_connection('host');c.assert_called_once_with(port='COM4');self.assertEqual(label,'USB COM4')
    def test_wifi_when_no_usb(self):
        with patch('diagnostic.list_ports.comports',return_value=[]),patch('diagnostic.Connection') as c:
            choose_connection('host');c.assert_called_once_with(host='host')
    def test_wifi_if_usb_cannot_open(self):
        with patch('diagnostic.list_ports.comports',return_value=[SimpleNamespace(vid=0x10c4,pid=0xea60,device='COM4')]),patch('diagnostic.Connection',side_effect=[OSError(),object()]) as c:
            _,label=choose_connection('host');self.assertEqual(label,'Wi-Fi host');self.assertEqual(c.call_count,2)
if __name__=='__main__':unittest.main()
