import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import risk_scenario_verification as suite


class Reporting(unittest.TestCase):
    def test_counts_and_evidence_follow_the_actual_executed_entries(self):
        control={'title':'boundary','test':'example.boundary','passed':True,'tests':1}
        prepared=[{'scenario':'A','native_html':True,'html':'<html>A</html>'},
                  {'scenario':'B','native_html':True,'html':'<html>B</html>'},
                  {'scenario':'C','native_html':False,'controls':[control]}]
        summary=suite.scenario_summary(prepared)
        self.assertEqual(summary['scenario_count'],3)
        self.assertEqual(summary['native_scenarios'],2)
        self.assertEqual(summary['control_checks'],1)
        self.assertEqual(summary['evidence']['native_ids'],['A','B'])
        self.assertEqual(summary['evidence']['control_tests'],['example.boundary'])
        smaller=suite.scenario_summary(prepared[1:])
        self.assertEqual(smaller['scenario_count'],2)
        self.assertEqual(smaller['native_scenarios'],1)
        self.assertFalse(smaller['generation_quality_evaluated'])
        self.assertFalse(smaller['recipient_inbox_verified'])

    def test_missing_or_failed_control_cannot_be_counted_as_passed(self):
        for control in ({'test':'unrun','passed':False,'tests':1},{'test':'unrun','passed':True,'tests':0}):
            with self.assertRaisesRegex(RuntimeError,'미실행·실패'):
                suite.scenario_summary([{'native_html':False,'controls':[control]}])

    def test_duplicate_evidence_and_empty_html_do_not_inflate_coverage(self):
        entry={'scenario':'A','native_html':True,'html':'<html>A</html>'}
        with self.assertRaisesRegex(RuntimeError,'중복'):
            suite.scenario_summary([entry,entry])
        control={'test':'same.method','passed':True,'tests':1}
        with self.assertRaisesRegex(RuntimeError,'중복'):
            suite.scenario_summary([{'native_html':False,'controls':[control,control]}])
        with self.assertRaisesRegex(RuntimeError,'HTML 내용'):
            suite.scenario_summary([{'scenario':'empty','native_html':True,'html':''}])

    def test_offline_cli_reports_actual_prepared_evidence_and_does_not_send(self):
        prepared=[{'scenario':'A','native_html':True,'html':'<html>fixture</html>'}]
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ,{'GITHUB_RUN_ATTEMPT':'1','EMAIL_RECEIVER':'','EMAIL_CC':'','ANTHROPIC_API_KEY':''}), \
                patch.object(suite,'snapshot',return_value={'fixture':'fixed'}), \
                patch.object(suite,'prepare',return_value=prepared), \
                patch.object(suite.smtplib,'SMTP_SSL') as smtp, \
                patch.object(suite,'Path',side_effect=lambda value:Path(directory) if value=='/tmp' else Path(value)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            suite.run({'operation':'scenario-suite','self_test':True,'request_id':'report-test','send':False})
            summary=json.loads(output.getvalue())
            self.assertEqual(summary['scenarios'],1)
            self.assertEqual(summary['native_previews'],1)
            self.assertEqual(summary['control_checks'],0)
            self.assertEqual(summary['evidence']['native_ids'],['A'])
            smtp.assert_not_called()


if __name__=='__main__':
    unittest.main()
