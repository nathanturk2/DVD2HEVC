"""Run the production BD-J mouse handlers with the stock libbluray API."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class MouseInputTests(unittest.TestCase):
    def test_production_xlet_mouse_and_directional_navigation(self):
        from dvd2uhd.author import api_jar, build_java
        if not shutil.which('javac') or not shutil.which('java'):
            self.skipTest('A Java JDK is required for BD-J event tests')
        try:
            api = api_jar(os.environ.get('DVD2HEVC_BDJ_API'))
        except RuntimeError as error:
            self.skipTest(str(error))
        sources = Path(__file__).parent / 'java'
        with tempfile.TemporaryDirectory() as folder:
            build_java(folder, api)
            cp = os.pathsep.join([folder, str(api)])
            options = dict(capture_output=True, text=True, timeout=60)
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW
            compiled = subprocess.run(['javac', '-source', '7', '-target', '7', '-cp', cp,
                                       '-d', folder, *map(str, sources.rglob('*.java'))], **options)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run(['java', '-Djava.awt.headless=true', '-cp', cp,
                                     'org.dvd2uhd.MouseInputTest'], **options)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('BD-J mouse input tests passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
