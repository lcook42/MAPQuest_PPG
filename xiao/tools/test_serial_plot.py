"""Offline parser tests: python -m unittest discover -s tools."""
import unittest
from serial_plot import parse_row


class ParseTests(unittest.TestCase):
    def test_waveform(self):
        self.assertEqual(parse_row('RedPPG:-1.2500\tIRPPG:0.0000\tGreenPPG:2.0000'),
                         {'RedPPG': -1.25, 'IRPPG': 0.0, 'GreenPPG': 2.0})

    def test_calibration(self):
        self.assertEqual(parse_row('ReferenceMAP:85.00\tPredictedMAP:86.75'),
                         {'ReferenceMAP': 85.0, 'PredictedMAP': 86.75})

    def test_diagnostics_not_graph_rows(self):
        for line in ['COUNT THIS MEASUREMENT: YES', 'TUNE 1 | mean=120000',
                     'RelativeOpticalDistance:2.0000', 'PLOT_COMPLETE',
                     'RedPPG:1.0000\tIRPPG:2.0000', '']:
            self.assertIsNone(parse_row(line))


if __name__ == '__main__':
    unittest.main()
