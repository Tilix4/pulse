from pulse.api import *
import unittest
import subprocess

# TODO : test trashing an open file

test_dir = os.path.dirname(__file__)
db = os.path.join(test_dir, "DB")
user_work = os.path.join(test_dir, "works")
user_products = os.path.join(test_dir, "products")
repos = os.path.join(test_dir, "repos")

cli_path = r"C:\Users\dddje\PycharmProjects\pulse\cli\cli.py"
python_exe = "c:\\python27\\python.exe"


def reset_files():
    for directory in [db, user_products, user_work, repos]:
        for path, subdirs, files in os.walk(directory):
            for name in files:
                filepath = os.path.join(path, name)
                if filepath.endswith(".pipe"):
                    os.chmod(filepath, 0o777)
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print('Failed to delete %s. Reason: %s' % (file_path, e))
    print "FILES RESET"

def cli_cmd_list(cmd_list):
    cmd = python_exe + " " + cli_path
    for arg in cmd_list:
        cmd += " " + arg
    subprocess.call(cmd)


class TestBasic(unittest.TestCase):
    def setUp(self):
        reset_files()

    # def tearDown(self):
    #     reset_files()

    def test_create_project(self):
        project_name = 'the_project'
        repository_parameters = "{'root': '" + os.path.join(repos, 'default') + "'}"
        cli_cmd_list([
            'create_project',
            project_name,
            db,
            user_work,
            user_products,
            '--repository_parameters "' + repository_parameters + '"'
        ])
        # check the project exists in db directory
        self.assertTrue(os.path.exists(os.path.join(db, project_name)))


if __name__ == '__main__':
    unittest.main()
    reset_files()
