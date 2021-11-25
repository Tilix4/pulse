"""
Created on 07 September 2020
@author: Jean Francois Sarazin
"""

import os
import glob
import pulse.file_utils as fu
import shutil
import time
try:
    import importlib.util
except ImportError:
    import imp
from pulse.database_adapters.interface_class import *
from pulse.exception import *
import pulse.config as cfg
import pulse.uri_standards as uri_standards
import tempfile
from datetime import datetime
import sys
import ctypes
import json
import subprocess


class PulseDbObject:
    """
        abstract class for all objects which need to save persistent data
    """
    def __init__(self, project, uri):
        self.uri = uri
        """universal resource identifier - string"""
        self.project = project
        """the project this object rely to - Project"""
        self.metas = {}
        """Store custom attributes, passed to repository - dict"""
        self._storage_vars = []
        """list of attributes name saved in db - list"""

    def _db_update(self, attribute_list):
        """
            save selected attributes value to database

            :param: attribute_list: attributes name which will be saved to database
            :type: attribute_list: list
        """
        data = dict((name, getattr(self, name)) for name in attribute_list)
        self.project.cnx.db.update(self.project.name, self.__class__.__name__, self.uri, data)

    def db_read(self):
        """
            read all object attributes from database.

            Will pass if the database have an attribute missing on the object

            :return: PulseDbObject
            :rtype: PulseDbObject
        """
        data = self.project.cnx.db.read(self.project.name, self.__class__.__name__, self.uri)
        for k in data:
            if k not in vars(self):
                continue
            setattr(self, k, data[k])
        return self

    def db_create(self):
        """
            initialize the object in database

            use all the attributes lists in ._storage.vars and save them to the DB
            the key is the uri
            raise DbError if the object already exists
        """
        data = dict((name, getattr(self, name)) for name in self._storage_vars)
        self.project.cnx.db.create(self.project.name, self.__class__.__name__, self.uri, data)


class Product:
    """
        abstract class for all products
    """
    def __init__(self, parent, product_type):
        self.uri = uri_standards.convert_from_dict({
            "entity": parent.resource.entity,
            "resource_type": parent.resource.resource_type,
            "product_type": product_type,
            "version": parent.version
        })
        self.abstract_uri = uri_standards.remove_version_from_uri(self.uri)
        self.parent = parent
        self.product_type = product_type
        self.directory = os.path.join(parent.get_products_directory(), product_type)
        self.product_users_file = ""

    def init_local_data_file(self):
        """
        create the local data file on user space
        :return:
        """
        if not os.path.isfile(self.product_users_file):
            fu.json_list_init(self.product_users_file)

    def add_product_user(self, user_directory):
        """
        add a local resource or product as product's user

        :param user_directory: the resource path
        """
        fu.json_list_append(self.product_users_file, user_directory)

    def remove_product_user(self, user_directory):
        """
        remove the specified local resource or product from the product's user

        :param user_directory: the resource path
        """
        fu.json_list_remove(self.product_users_file, user_directory)

    def get_product_users(self):
        """
        return the list of local resources or product using this product

        :return: resources filepath list
        """
        return fu.json_list_get(self.product_users_file)

    def get_unused_time(self):
        """
        return the time since the local product has not been used by any resource or product.
        Mainly used to purge local products from pulse's cache

        :return: time value
        """
        if not os.path.exists(self.directory):
            return -1
        users = self.get_product_users()
        if users:
            return -1
        if os.path.exists(self.product_users_file):
            return time.time() - os.path.getmtime(self.product_users_file) + 0.01
        else:
            return time.time() - os.path.getctime(self.directory)


class CommitProduct(PulseDbObject, Product):
    """
        Product which has been published to database
    """
    def __init__(self, parent, product_type):
        Product.__init__(self, parent, product_type)
        PulseDbObject.__init__(self, parent.project, self.uri)
        self.products_inputs = []
        self._storage_vars = ['product_type', 'products_inputs', 'uri']
        self.product_users_file = os.path.normpath(os.path.join(
            self.parent.project.commit_product_data_directory,
            fu.uri_to_json_filename(self.uri)
        ))

    def download(self):
        """
        download the product to local pulse cache if needed

        :return: the product's local filepath
        """
        if os.path.exists(self.directory):
            return self.directory

        self.project.cnx.repositories[self.parent.resource.repository].download_product(self)
        if not os.path.exists(self.parent.pulse_filepath):
            open(self.parent.pulse_filepath, 'a').close()

        self.init_local_data_file()

        fu.write_data(self.product_users_file, [])
        for uri in self.products_inputs:
            product = self.project.get_product(uri)
            product.download()
            product.add_product_user(self.directory)
        return self.directory

    def unregister_to_user_products(self):
        """
        unregister the product to the user local products list
        """
        os.remove(self.product_users_file)

    def remove_from_local_products(self, recursive_clean=False):
        """
        remove the product from local pulse cache
        will raise a pulse error if the product is used by a resource
        will raise an error if the product's folder is locked by the filesystem
        """
        if len(self.get_product_users()) > 0:
            raise PulseError("Can't remove a product still in use")

        # test the folder can be moved
        if not fu.test_path_write_access(self.directory):
            raise PulseError("folder is in used by a process : " + self.directory)

        # unregister from its inputs
        for uri in self.products_inputs:
            product_input = self.project.get_product(uri)
            product_input.remove_product_user(self.directory)
            if recursive_clean:
                try:
                    product_input.remove_from_local_products(recursive_clean=True)
                except PulseError:
                    pass

        shutil.rmtree(self.directory)
        # remove also the version directory if it's empty now
        version_dir = os.path.dirname(self.directory)
        if os.listdir(version_dir) == [cfg.pulse_filename]:
            shutil.rmtree(version_dir)
            parent_dir = os.path.dirname(version_dir)
            if not os.listdir(parent_dir):
                shutil.rmtree(parent_dir)
        self.unregister_to_user_products()


class Commit(PulseDbObject):
    """
        Object created when a resource has been published to database
        The commit is a versioned resource
    """
    def __init__(self, resource, version):
        self.uri = resource.uri + "@" + str(version)
        PulseDbObject.__init__(self, resource.project, self.uri)
        self.resource = resource
        self.comment = ""
        self.files = {}
        self.products_inputs = []
        self.version = int(version)
        self.products = []
        self.pulse_filepath = os.path.join(self.get_products_directory(), cfg.pulse_filename)
        """ list of product names"""
        self._storage_vars = ['version', 'products', 'files', 'comment', 'products_inputs']

    def get_product(self, product_type):
        """
        return the commit's product with the specified product type. If the product doesn't exists return a
        pulseDataBaseMissingObject exception

        :param product_type: string
        :return: a CommitProduct
        """
        return CommitProduct(self, product_type).db_read()

    def get_products(self):
        """
        return the commit's products list
        """
        products = []
        for product_name in self.products:
            products.append(CommitProduct(self, product_name))
        return products

    def get_products_directory(self):
        """
        return the commit's products directory

        :return: filepath
        """
        return self.resource.get_products_directory(self.version)


class WorkNode:
    """
        abstract class for unpublished data (work or product)
    """
    def __init__(self, project, directory):
        self.directory = directory
        self.products_inputs_file = os.path.join(directory, "product_inputs.json")
        self.project = project

    def get_inputs(self):
        """
        return a dict of work node inputs in the form
        {uri_input_name : immutable_uri}

        :return: products uri list
        """
        if not os.path.exists(self.products_inputs_file):
            return {}
        with open(self.products_inputs_file, "r") as read_file:
            return json.load(read_file)

    def add_input(self, uri):
        """
        add a product to work inputs list
        download it to local product if needed
        uri can be mutable (ie: anna-mdl.abc) or not (ie : anna-mdl.abc@4)
        if a mutable uri is given, the last version will be used

        :param uri: the product's uri, could be a mutable uri
        """
        if not uri_standards.is_valid(uri):
            raise PulseError("malformed uri : " + uri)

        # if the uri is mutable, remove the version label. Labels are not supported
        if uri_standards.is_mutable(uri):
            uri = uri_standards.remove_version_from_uri(uri)

        # check input already exists
        inputs = self.get_inputs()
        if uri in inputs:
            raise PulseError("input already exists : " + uri)

        # update input and register to it
        if uri_standards.is_mutable(uri):
            self.update_input(uri)
        else:
            self.update_input(uri, uri)

    def update_input(self, uri, target_uri=None):
        """
        update a work input.
        if a target uri is set, the input will now point to this uri
        if no target uri is set, the input will look for a newer product version
        the product is downloaded if needed
        the input link is redirected to the new product
        the product register the work as a new user
        :param uri: the work's input uri
        :param target_uri: force the update to a defined uri

        """
        # if uri is not forced to a version, use the default uri
        if not target_uri:
            target_uri = uri

        product = self.project.get_product(target_uri)
        if isinstance(product, CommitProduct):
            product.download()

        if self.__class__.__name__ == "Work":
            input_directory = os.path.join(self.directory, cfg.work_input_dir, uri)

            if os.path.exists(input_directory):
                os.remove(input_directory)
            fu.make_directory_link(input_directory, product.directory)

        # save updated input entry to disk
        inputs = self.get_inputs()
        inputs[uri] = product.uri
        with open(self.products_inputs_file, "w") as write_file:
            json.dump(inputs, write_file, indent=4, sort_keys=True)

        product.add_product_user(self.directory)

    def remove_input(self, uri):
        """
        remove a product from object's inputs list

        :param uri: input uri
        """
        inputs = self.get_inputs()
        if uri not in inputs:
            raise PulseError("input does not exist : " + uri)

        inputs.pop(uri, None)
        with open(self.products_inputs_file, "w") as write_file:
            json.dump(inputs, write_file, indent=4, sort_keys=True)

        product = self.project.get_product(uri)
        product.remove_product_user(self.directory)

        # remove linked input directory
        os.remove(os.path.join(self.directory, cfg.work_input_dir, uri))


class WorkProduct(Product, WorkNode):
    """
        class for products which has not been registered to database yet
    """
    def __init__(self, work, product_type):
        Product.__init__(self, work, product_type)
        WorkNode.__init__(self, work.project, self.directory)
        self.product_users_file = os.path.normpath(os.path.join(
            self.parent.project.work_product_data_directory,
            fu.uri_to_json_filename(self.uri)
        ))


class Work(WorkNode):
    """
        Resource downloaded locally to be modified
    """
    def __init__(self, resource):
        WorkNode.__init__(self, resource.project, resource.sandbox_path)
        self.resource = resource
        self.version = None
        self.data_file = os.path.join(self.project.work_data_directory, fu.uri_to_json_filename(self.resource.uri))

    def _check_exists_in_user_workspace(self):
        if not os.path.exists(self.directory):
            raise PulseMissingNode("Missing work space : " + self.directory)

    def _get_trash_directory(self):
        date_time = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
        path = os.path.join(self.project.cfg.get_work_user_root(), self.project.name, "TRASH") + os.sep
        path += self.resource.uri + "-" + date_time
        return path

    def _get_work_files(self):
        files_dict = {}
        excluded_path = [cfg.work_output_dir, cfg.work_input_dir]
        for root, dirs, files in os.walk(self.directory, topdown=True):
            dirs[:] = [d for d in dirs if d not in excluded_path]
            for f in files:
                filepath = os.path.join(root, f)
                relative_path = filepath[len(self.directory):]
                files_dict[relative_path.replace(os.sep, "/")] = {"checksum": fu.md5(filepath)}
        return files_dict

    def get_product(self, product_type):
        """
        return the resource's work product based on the given type

        :param product_type:
        :return: a work product
        """
        if product_type not in self.list_products():
            raise PulseError("product not found : " + product_type)
        return WorkProduct(self, product_type)

    def list_products(self):
        """
        return the work's product's type list

        :return: a string list
        """
        return fu.read_data(self.data_file)["outputs"]

    def create_product(self, product_type):
        """
        create a new product for the work

        :param product_type: string
        :return: the new work product object
        """
        self._check_exists_in_user_workspace()
        outputs = self.list_products()
        if product_type in outputs:
            raise PulseError("product already exists : " + product_type)
        work_product = WorkProduct(self, product_type)
        # create the pulse data file
        work_product.init_local_data_file()

        os.makedirs(work_product.directory)
        pulse_filepath = os.path.join(self.get_products_directory(), cfg.pulse_filename)
        if not os.path.exists(pulse_filepath):
            open(pulse_filepath, 'a').close()
        # update work pipe file with the new output
        outputs.append(product_type)
        data_dict = fu.read_data(self.data_file)
        data_dict["outputs"] = outputs
        fu.write_data(self.data_file, data_dict)

        return work_product

    def trash_product(self, product_type):
        """
        move the specified product to the trash directory
        raise an error if the product is used by a resource or another product

        :param product_type: string
        """
        self._check_exists_in_user_workspace()
        if product_type not in self.list_products():
            raise PulseError("product does not exists : " + product_type)
        product = WorkProduct(self, product_type)

        if not fu.test_path_write_access(product.directory):
            raise PulseError("can't move folder " + product.directory)

        users = product.get_product_users()
        if users:
            raise PulseError("work can't be trashed if its product is used : " + users[0])

        # unregister from products
        for input_product_uri in product.get_inputs():
            input_product = self.project.get_product(input_product_uri)
            if os.path.exists(input_product.directory):
                input_product.remove_product_user(product.directory)

        # create the trash work directory
        trash_directory = self._get_trash_directory()
        if not os.path.exists(trash_directory):
            os.makedirs(trash_directory)

        # move folder
        shutil.move(product.directory, os.path.join(trash_directory, "PRODUCTS", product_type))

        # remove the product from work outputs
        data_dict = fu.read_data(self.data_file)
        data_dict["outputs"].remove(product_type)
        fu.write_data(self.data_file, data_dict)

        # remove the products directory if it's empty
        products_directory = self.get_products_directory()
        if not os.listdir(products_directory):
            shutil.rmtree(products_directory)

        # remove the product from products local data
        os.remove(product.product_users_file)

    def write(self):
        """
        write the work object to user workspace
        """
        # create work folder if needed
        if not os.path.exists(self.directory):
            os.makedirs(self.directory)

        # write data to json
        fu.write_data(self.data_file, {
            "version": self.version,
            "entity": self.resource.entity,
            "resource_type": self.resource.resource_type,
            "outputs": [],
            "work_files": self._get_work_files()
            })

        # create work product directory
        work_product_directory = self.get_products_directory()
        os.makedirs(work_product_directory)

        # create junction point to the product directory
        work_output_path = os.path.join(self.directory, cfg.work_output_dir)

        # link work output directory to its current output product directory
        fu.make_directory_link(work_output_path, work_product_directory)

        work_input_path = os.path.join(self.directory, cfg.work_input_dir)
        if not os.path.exists(work_input_path):
            os.makedirs(work_input_path)

    def read(self):
        """
        read the work data from user work space
        if the work doesn't exists in user work space, raise a pulse error

        :return: the updated work
        """
        if not os.path.exists(self.data_file):
            raise PulseError("work does not exists : " + self.directory)
        work_data = fu.read_data(self.data_file)
        self.version = work_data["version"]
        return self

    def commit(self, comment="", keep_products_in_cache=True, recreate_last_products=True):
        """
        commit the work to the repository, and publish it to the database

        :param comment: a user comment string
        :param recreate_last_products: keep same output products after the commit
        :param keep_products_in_cache: keep the commit products in local cache after the commit
        :return: the created commit object
        """
        self._check_exists_in_user_workspace()
        # check current the user permission
        if self.resource.user_needs_lock():
            raise PulseError("resource is locked by another user : " + self.resource.lock_user)

        # check the work is up to date
        expected_version = self.resource.last_version + 1
        if not self.version == expected_version:
            raise PulseError("Your version is deprecated, it should be based on " + str(self.resource.last_version))

        # check the work status
        if not self.status():
            raise PulseError("no file change to commit")

        # check all inputs are registered
        for linked_uri, uri in self.get_inputs().items():
            try:
                self.project.get_product(uri)
            except PulseDatabaseMissingObject:
                raise PulseError("Input should be commit first : " + uri)

        # lock the resource to prevent concurrent commit
        lock_state = self.resource.lock_state
        lock_user = self.resource.lock_user
        self.resource.set_lock(True, self.project.cnx.user_name + "_commit", steal=True)

        # copy work files to a new version in repository
        commit = Commit(self.resource, self.version)
        commit.files = self._get_work_files()
        commit.project.cnx.repositories[self.resource.repository].upload_resource_commit(
            commit, self.directory, commit.files, self.get_products_directory())

        # register changes to database
        commit.comment = comment
        commit.products_inputs = self.get_inputs()
        commit.products = self.list_products()

        # convert work products to commit products
        if commit.products:
            for product_type in commit.products:
                work_product = self.get_product(product_type)

                commit_product = CommitProduct(commit, product_type)
                commit_product.products_inputs = work_product.get_inputs()
                commit_product.db_create()

                if not keep_products_in_cache:
                    commit_product.remove_from_local_products()
                else:
                    # change the work product data file to a commit product file
                    os.rename(work_product.product_users_file, commit_product.product_users_file)

        commit.db_create()
        self.resource.set_last_version(self.version)

        # increment the work and the products files
        self.version += 1
        self.write()

        # recreate same products
        if recreate_last_products:
            for product in commit.products:
                self.create_product(product)

        # restore the resource lock state
        self.resource.set_lock(lock_state, lock_user, steal=True)

        return commit

    def revert(self):
        """
        revert local changes to the work and its product directory

        :return: True on success
        """
        # trash the current content
        self.trash(no_backup=True)
        # checkout the last work commit version
        self.resource.checkout(index=self.version - 1)
        return True

    def update(self):
        """
        update local work copy to the last resource commit
        fails if there's some local changes

        :return: True on success
        """
        # test there's no changes that could be lost
        if self.status():
            raise PulseError("local changes detected, you should commit or revert your work first")
        # delete the work
        self.trash(no_backup=True)
        # checkout the last resource commit version
        self.resource.checkout()
        return True

    def trash(self, no_backup=False):
        """
        remove the work from user workspace

        :param no_backup: if False, the work folder is moved to trash directory. If True, it is removed from disk
        :return: True on success
        """
        self._check_exists_in_user_workspace()
        # test the work and products folder are movable
        products_directory = self.get_products_directory()
        for path in [self.directory, products_directory]:
            if os.path.exists(path) and not fu.test_path_write_access(path):
                raise PulseError("can't move folder " + path)

        # check workProducts are not in use
        for product_type in self.list_products():
            self.trash_product(product_type)

        # unregister from products
        for input_product_uri in self.get_inputs():
            input_product = self.project.get_product(input_product_uri)
            if os.path.exists(input_product.directory):
                input_product.remove_product_user(self.directory)

        # create the trash work directory
        trash_directory = self._get_trash_directory()
        if not os.path.exists(trash_directory):
            os.makedirs(trash_directory)

        # remove work output link
        work_output = os.path.join(self.directory, cfg.work_output_dir)
        if os.path.exists(work_output):
            os.remove(work_output)

        # move work product directory
        if os.path.exists(products_directory):
            shutil.move(products_directory,  os.path.join(trash_directory, "PRODUCTS"))

        # move work files
        shutil.move(self.directory, trash_directory + "/work")

        if no_backup:
            shutil.rmtree(trash_directory)

        # recursively remove products directories if they are empty
        fu.remove_empty_parents_directory(
            os.path.dirname(products_directory),
            [self.project.cfg.get_product_user_root()]
        )

        # remove work data file
        os.remove(self.data_file)

        return True

    def version_pipe_filepath(self, index):
        """
        get the pipe file path

        :param index:
        :return: filepath
        """
        return os.path.join(
            self.directory,
            cfg.DEFAULT_VERSION_PREFIX + str(index).zfill(cfg.DEFAULT_VERSION_PADDING) + ".pipe"
        )

    def status(self):
        """
        return the work files changes since last commit. Based on the files modification date time

        :return: a list a tuple with the filepath and the edit type (edited, removed, added)
        """

        diff = fu.compare_directory_content(
            self._get_work_files(),
            fu.read_data(self.data_file)["work_files"]
        )
        for product_name in self.list_products():
            product_directory = os.path.join(self.get_products_directory(), product_name)
            for root, subdirectories, files in os.walk(product_directory):
                for f in files:
                    filepath = os.path.join(root, f)
                    relative_path = filepath[len(product_directory):]
                    diff["product-" + product_name + relative_path] = "added"
        return diff

    def get_products_directory(self):
        """
        return the work products directory

        :return: filepath
        """
        return self.resource.get_products_directory(self.version)


class Resource(PulseDbObject):
    """
        a project's resource. A resource is meant to generate products, and use products from other resources
    """
    def __init__(self, project, entity, resource_type):
        self.lock_state = False
        self.lock_user = ''
        self.last_version = 0
        self.resource_type = resource_type
        self.entity = entity
        self.repository = None
        self.resource_template = ''
        PulseDbObject.__init__(
            self,
            project,
            uri_standards.convert_from_dict({"entity": entity, "resource_type": resource_type})
        )
        self.sandbox_path = os.path.join(
            project.cfg.get_work_user_root(), project.name, self.uri)
        self._storage_vars = [
            'lock_state', 'lock_user', 'last_version', 'resource_type', 'entity', 'repository', 'metas']

    def get_products_directory(self, version_index):
        """
        return products filepath of the given resource version

        :param version_index: integer
        :return: string
        """
        version = str(version_index).zfill(cfg.DEFAULT_VERSION_PADDING)
        path = os.path.join(
            self.project.cfg.get_product_user_root(),
            self.project.name,
            self.uri,
            cfg.DEFAULT_VERSION_PREFIX + version
        )
        return path

    def set_last_version(self, version):
        """
        set resource last version index

        :param version: integer
        """
        self.last_version = version
        self._db_update(["last_version"])

    def user_needs_lock(self, user=None):
        """
        return if the given user needs to get the resource lock to modify it
        if no user is specified, the current connexion user will be used

        :param user: string
        :return: boolean
        """
        if not user:
            user = self.project.cnx.user_name
        return self.lock_state and self.lock_user != user

    def get_index(self, version_name):
        """
        given an index or a tag, return the corresponding version number.
        accepted tag : "last"

        :param version_name: string or integer
        :return: integer
        """
        try:
            instance_type = basestring
        except NameError:
            instance_type = str
        if isinstance(version_name, instance_type):
            if version_name == "last":
                return self.last_version
            else:
                try:
                    return int(version_name)
                except ValueError:
                    raise PulseError("unsupported version name")
        return version_name

    def get_commit(self, version):
        """
        get the commit object from the given version number

        :param version: integer
        :return: Commit
        """
        return Commit(self, self.get_index(version)).db_read()

    def get_work(self):
        """
        get the Work object associated to the resource.
        IF there's no current work in user work space, return None

        :return:
        """
        try:
            return Work(self).read()
        except PulseError:
            return None

    def checkout(self, index="last", destination_folder=None, recreate_products=True):
        """
        Download the resource work files in the user work space.
        Download related dependencies if they are not available in user products space
        :param recreate_products: recreate the products from the source commit
        :param destination_folder: where the resource will be checkout, if not set, project config is used
        :param index: the commit index to checkout. If not set, the last one will be used
        """
        if not os.path.exists(self.project.cfg.get_work_user_root()):
            self.project.initialize_sandbox()

        work = Work(self)

        # abort checkout if the work already exists in user sandbox, just rebuild its data
        if os.path.exists(work.data_file):
            return Work(self).read()

        if not destination_folder:
            destination_folder = self.sandbox_path

        # create the work object
        work.version = self.last_version + 1
        source_resource = None
        source_commit = None
        out_product_list = []

        # if it's an initial checkout, try to get data from source resource or template. Else, create empty folders
        if self.last_version == 0:
            # if a source resource is given, get its template
            if self.resource_template != '':
                template_dict = uri_standards.convert_to_dict(self.resource_template)
                source_resource = self.project.get_resource(template_dict['entity'], template_dict['resource_type'])
                source_commit = source_resource.get_commit("last")
            else:
                # try to find a template
                try:
                    if self.entity != cfg.template_name:
                        source_resource = self.project.get_resource(cfg.template_name, self.resource_type)
                        source_commit = source_resource.get_commit("last")
                except PulseDatabaseMissingObject:
                    pass

        # else get the resource commit
        else:
            source_resource = self
            source_commit = self.get_commit(index)

        # if no source has been found, just create empty work folder
        if not source_commit:
            os.makedirs(destination_folder)
        else:
            self.project.cnx.repositories[source_resource.repository].download_work(source_commit, destination_folder)
            out_product_list = source_commit.products

        work.write()
        # recreate empty output products
        if recreate_products:
            for product in out_product_list:
                work.create_product(product)

        # download requested input products if needed
        for input_name, uri in work.get_inputs().items():
            work.update_input(input_name, uri)

        return work

    def set_lock(self, state, user=None, steal=False):
        """
        change the lock state, and the lock user.
        raise a pulse error if the resource is already locked by someone else, except the steal argument is True.

        :param state: boolean
        :param user: string
        :param steal: boolean
        """
        # abort if the resource is locked by someone else and the user doesn't want to steal the lock
        if not steal:
            self.db_read()
            if self.user_needs_lock(user):
                return

        self.lock_state = state
        if not user:
            self.lock_user = self.project.cnx.user_name
        else:
            self.lock_user = user
        self._db_update(['lock_user', 'lock_state'])

    def set_repository(self, new_repository):
        """
        move the resource to another repository.

        :param new_repository: Repository
        """
        if self.repository == new_repository:
            raise PulseError("the destination repository have to be different as current one :" + new_repository)

        if self.user_needs_lock():
            raise PulseError("you can't move a resource locked by another user :" + self.lock_user)

        # lock the resource to prevent concurrent commit
        lock_state = self.lock_state
        lock_user = self.lock_user
        self.set_lock(True, self.project.cnx.user_name + "_set_repo", steal=True)

        temp_directory = tempfile.mkdtemp()
        if new_repository not in self.project.cnx.repositories:
            raise PulseError("unknown repository : " + new_repository)
        self.project.cnx.repositories[self.repository].download_resource(self, temp_directory)
        self.project.cnx.repositories[new_repository].upload_resource(self, temp_directory)
        self.project.cnx.repositories[self.repository].remove_resource(self)
        self.repository = new_repository
        self.set_lock(lock_state, lock_user, steal=True)
        self._db_update(['repository'])


class Config(PulseDbObject):
    """
        project's configuration stored in database
    """
    def __init__(self, project):
        self.work_user_root = None
        self.product_user_root = None
        self.default_repository = None
        self._storage_vars = vars(self).keys()
        PulseDbObject.__init__(self, project, "config")
        self._storage_vars = [
            "work_user_root",
            "product_user_root",
            "default_repository",
        ]

    def get_work_user_root(self):
        return os.path.expandvars(self.work_user_root)

    def get_product_user_root(self):
        return os.path.expandvars(self.product_user_root)

    def save(self):
        """
        save the project configuration to database
        """
        self._db_update(self._storage_vars)


class Project:
    """
        a Pulse project, containing resources and a configuration
    """
    def __init__(self, connection, project_name):
        self.cnx = connection
        self.name = project_name
        self.cfg = Config(self)
        self.work_directory = None
        self.work_data_directory = None
        self.commit_product_data_directory = None
        self.work_product_data_directory = None

    def get_product(self, uri_string):
        """
        return the product corresponding of the given uri
        @last return the last version
        @work return the local work version
        raise a PulseError if the uri is not found in the project

        :param uri_string: a pulse product uri
        :return: Product
        """
        uri_dict = uri_standards.convert_to_dict(uri_string)
        resource = Resource(self, uri_dict['entity'], uri_dict['resource_type'])
        resource.db_read()
        if not uri_dict['version']:
            uri_dict['version'] = "last"

        # TODO : error here. If we find a commit V02 with the right product, it will be returned.
        #  We should compare the work it could be a v03
        if uri_dict['version'] == "last":
            version = 0
            products = self.cnx.db.find_uris(
                self.name,
                "CommitProduct",
                uri_standards.remove_version_from_uri(uri_string) + "@*"
            )
            if products:
                products.sort()
                last_product_uri = products[-1]
                version = uri_standards.convert_to_dict(last_product_uri)['version']
            current_work = resource.get_work()
            if current_work and current_work.version > int(version) and uri_dict["product_type"] in current_work.list_products():
                product_parent = current_work
            else:
                product_parent = resource.get_commit(uri_standards.convert_to_dict(last_product_uri)['version'])
            if version == 0:
                raise PulseError("No product found for this resource :" + uri_string)
        else:
            resource.get_index(uri_dict['version'])
            index = resource.get_index(uri_dict['version'])
            product_parent = resource.get_commit(index)

        return product_parent.get_product(uri_dict["product_type"])

    def list_products(self, uri_pattern):
        """
        return a product objects list matching the uri pattern.
        The pattern should be in the glob search type

        :param uri_pattern: string
        :return: a Products list
        """
        return [self.get_product(uri) for uri in self.cnx.db.find_uris(self.name, "CommitProduct", uri_pattern)]

    def get_local_commit_products(self):
        """
        return the list of products in user work space
        :return: uri list
        """
        if not os.path.exists(self.commit_product_data_directory):
            return []
        file_list = os.listdir(self.commit_product_data_directory)
        return [fu.json_filename_to_uri(filename) for filename in file_list]

    def get_local_works(self, uri_pattern="*"):
        """
        return the list of work resource in user sandbox
        :return: uri list
        """
        if not os.path.exists(self.work_data_directory):
            return []
        path_list = glob.glob(os.path.join(self.work_data_directory, uri_pattern) + ".json")
        file_list = [os.path.basename(x) for x in path_list]
        return [fu.json_filename_to_uri(filename) for filename in file_list]

    def purge_unused_user_products(self, unused_days=0, resource_filter=None, dry_mode=False):
        """
        remove unused products from the user product space, based on a unused time

        :param unused_days: for how many days this products have not been used by the user
        :param resource_filter: affect only products with the uri starting by the given string
        :param dry_mode: do not delete the unused products
        :return: purge products list
        """
        purged_products = []
        for uri in self.get_local_commit_products():
            if resource_filter:
                if not uri.startswith(resource_filter.uri):
                    continue

            product = self.get_product(uri)
            if product.get_unused_time() > (unused_days*86400):
                purged_products.append(product.uri)
                if not dry_mode:
                    product.remove_from_local_products(recursive_clean=True)
        return purged_products

    def load_config(self):
        """
        load the project configuration from database
        """
        self.cfg.db_read()
        self.work_directory = os.path.join(self.cfg.get_work_user_root(), self.name)
        self.work_data_directory = os.path.join(self.work_directory, cfg.pulse_data_dir, "works")
        product_root = os.path.join(self.cfg.get_product_user_root(), self.name)
        self.commit_product_data_directory = os.path.join(product_root, cfg.pulse_data_dir, "commit_products")
        self.work_product_data_directory = os.path.join(product_root, cfg.pulse_data_dir, "work_products")

    def initialize_sandbox(self):
        # create local data directories
        for directory in [self.work_data_directory,
                          self.commit_product_data_directory,
                          self.work_product_data_directory]:
            if not os.path.isdir(directory):
                os.makedirs(directory)
            # if platform is windows, hide the directory with ctypes
            if sys.platform == "win32":
                ctypes.windll.kernel32.SetFileAttributesW(os.path.dirname(directory), 2)

        # write connexion path and settings to local project settings
        json_path = os.path.join(self.work_directory, cfg.pulse_data_dir, cfg.project_settings)
        data = {'connection': self.cnx.get_settings()}
        with open(json_path, "w") as write_file:
            json.dump(data, write_file, indent=4, sort_keys=True)

    def get_resource(self, entity, resource_type):
        """
        return a project resource based on its entity name and its type
        will raise a PulseError on missing resource

        :param entity:
        :param resource_type:
        :return:
        """
        return Resource(self, entity, resource_type).db_read()

    def create_template(self, resource_type, repository=None, source_resource=None):
        return self.create_resource(cfg.template_name, resource_type, repository,  source_resource)

    def create_resource(self, entity, resource_type, repository=None, source_resource=None):
        """
        create a new project's resource

        :param entity: entity of this new resource. Entity is like a namespace
        :param resource_type:
        :param repository: a pulse Repository, if None, the project default repository is used
        :param source_resource: if given the resource content will be initialized with the given resource
        :return: the created resource object
        """

        resource = Resource(self, entity, resource_type)

        if not repository:
            repository = self.cfg.default_repository
        resource.repository = repository

        # if a source resource is given keep its uri to db
        if source_resource:
            try:
                source_resource.get_commit("last")
            except PulseDatabaseMissingObject:
                raise PulseError("no commit found for template : " + source_resource.uri)

            resource.resource_template = source_resource.uri

        resource.db_create()

        return resource


class Connection:
    """
        connection instance to a Pulse database
    """
    def __init__(self, adapter, path="", username="", password="", **settings):
        self.db = import_adapter("database", adapter).Database(path, username, password, settings)
        self.path = path
        self.user_name = self.db.get_user_name()
        self.repositories = self.get_repositories()
        self._adapter = adapter
        self._settings = settings

    def get_settings(self):
        return {'path': self.path, 'settings': self._settings, 'adapter': self._adapter}

    def get_repositories(self):
        repositories = {}
        db_repositories = self.db.get_repositories()
        for name in db_repositories:
            db_repo = db_repositories[name]
            repositories[name] = import_adapter("repository", db_repo['adapter']).Repository(
                db_repo['login'],
                db_repo['password'],
                db_repo['settings'])
        return repositories

    def get_projects(self):
        return self.db.get_projects()

    def create_project(self,
                       project_name,
                       work_user_root,
                       default_repository,
                       product_user_root,
                       ):
        """
        create a new project in the connexion database
        work user root and product user root have to be independent

        :param project_name:
        :param work_user_root: user work space path where the project directory will be created
        :param product_user_root: user product space path where the project directory will be created
        :param default_repository: repository name use by default when a resource is created
        :return: the new pulse Project
        """
        work_user_root = work_user_root.replace("\\", "/")
        product_user_root = product_user_root.replace("\\", "/")
        if work_user_root in product_user_root or product_user_root in work_user_root:
            raise PulseError("work user root and product user root should be independent")

        project = Project(self, project_name)
        self.db.create_project(project_name)
        project.cfg.default_repository = default_repository
        project.cfg.work_user_root = work_user_root
        project.cfg.product_user_root = product_user_root
        project.cfg.db_create()
        project.load_config()
        return project

    def get_project(self, project_name):
        """
        return a pulse project from the database

        :param project_name: the pulse project's name
        :return: Project
        """
        project = Project(self, project_name)
        try:
            project.load_config()
        except PulseDatabaseMissingObject:
            raise PulseError("Missing Project : " + project_name)
        return project

    def delete_project(self, project_name):
        self.db.delete_project(project_name)

    def add_repository(self, name, adapter, login="", password="", **settings):
        """
        add a new repository to the project.

        :param name: the new repository name
        :param adapter: must be an existing module from repository adapters.
        :param settings: dict containing the connection parameters passed to the module
        :param login: the repository login
        :param password: the repository password
        """
        if name in self.repositories:
            raise PulseError("Repository already exists : " + name)
        # test valid settings
        repository = import_adapter("repository", adapter).Repository(settings=settings, login=login, password=password)
        repository.test_settings()
        # write repo settings to db config
        self.repositories[name] = repository
        self.db.create_repository(name, adapter, login, password, settings)

    def edit_repository(self, name, adapter, login="", password="", **settings):
        """
        edit the repository property
        raise a PulseError if the repository is not found
        """
        if name not in self.repositories:
            raise PulseError("Repository does not exists : " + name)
        self.db.update(
            "_Config",
            "Repository",
            name,
            {"adapter": adapter, "settings": settings, "login": login, "password": password}
        )


def get_adapter_directory_path(adapter_type):
    pulse_filepath = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(pulse_filepath, adapter_type + "_adapters")


def get_adapter_list(adapter_type):
    files = [os.path.basename(x) for x in glob.glob(os.path.join(get_adapter_directory_path(adapter_type), "*.py"))]
    files.remove("interface_class.py")
    files.remove("__init__.py")
    return [os.path.splitext(x)[0] for x in files]


def import_adapter(adapter_type, adapter_name):
    """
    dynamically import a module adapter from plugins directory

    :param adapter_type: should be "database" or "repository"
    :param adapter_name:
    :return: the adapter module
    """
    path = os.path.join(get_adapter_directory_path(adapter_type), adapter_name + ".py")

    try:
        # python 3 import
        spec = importlib.util.spec_from_file_location(adapter_type, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except NameError:
        # python 2 import
        mod = imp.load_source(adapter_type, path)
    return mod


def get_project_from_path(path, username="", password=""):
    path = os.path.normpath(path)
    path_list = path.split(os.sep)
    mode = None

    # find the pulse_data_dir to determine if it's a product or work URI
    for i in range(1, len(path_list)):
        if os.path.exists(os.path.join(path, cfg.pulse_data_dir, "works")):
            mode = "work"
            break
        path = os.path.dirname(path)
    if not mode:
        raise PulseError("can't convert path to uri, no project found")
    project_name = path.split(os.sep)[-1]
    project_settings = fu.read_data(os.path.join(path, cfg.pulse_data_dir, cfg.project_settings))
    cnx = Connection(
        adapter=project_settings['connection']['adapter'],
        path=project_settings['connection']['path'],
        username=username,
        password=password,
        **project_settings['connection']['settings']
    )
    return cnx.get_project(project_name)
