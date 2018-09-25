#!/usr/bin/env python3

from argparse import ArgumentParser
from collections import OrderedDict
import json
import os
import subprocess
import sys
from tempfile import NamedTemporaryFile
import typing

from jinja2 import Environment, FileSystemLoader
from pycheckey import KeyEnsurer
import yaml

from dataclasses import dataclass


sys.tracebacklimit = 1


def parse_arguments():
    """
    Creates the Argument Parser for running from the
    command line, and returns the parsed args
    """
    parser = ArgumentParser()
    parser.add_argument(
        'directory',
        metavar='directory',
        help='The top level directory containing the templates.'
    )
    parser.add_argument(
        '--config', '-c',
        dest='config_path',
        default=os.getenv('CONFIG', 'config.json'),
        help='The path to the configuration file, if not in "directory".'
    )
    parser.add_argument(
        '--out', '-o',
        dest='out_file',
        default=None,
        help='Output the Packer manifest to a file.'
    )
    parser.add_argument(
        '--skip', '-s',
        dest='skip_build',
        action='store_true',
        help='Skip actually building the image using Packer.'
    )
    parser.add_argument(
        '--dry-run', '-d',
        dest='dry_run',
        action='store_true',
        help='Output the compiled manifest to the console, don\'t actually run anything.'
    )

    return parser.parse_args()


def _merge_dicts(source, destination):
    """
    Deeply merges two dictionaries, included nested
    keys, merging lists, and updating values.

    NOTE: source has precendence over duplicated keys
    """
    for key, value in source.items():
        if isinstance(value, dict):
            # get node or create one
            node = destination.setdefault(key, {})
            _merge_dicts(value, node)
        elif isinstance(value, list):
            if key in destination:
                destination[key].extend(value)
            else:
                destination[key] = value
        else:
            destination[key] = value

    return destination


class BuildException(Exception):
    """General exception raised for errors in the build process
    """
    pass


@dataclass
class Configuration:
    """Configuration object for managing the build process
    """

    config_file_path: str             # Where the config file is located
    root_directory: str               # The root directory for the templates
    template_paths: typing.List[str]  # A list of paths to the templates
    variable_paths: typing.List[str]  # A list of paths to the variables

    # Keys required in config file for the build process to run
    required_keys = [
        'templates',
        'variables'
    ]

    @staticmethod
    def build_config_file_path(root_directory, config_path):
        """Try to find the location of the config file.
        """
        path = os.path.join(root_directory, 'config.json')
        if os.path.isfile(path):
            return path
        if os.path.isfile(config_path):
            return config_path
        raise BuildException(f'Could not find the config file "{config_path}".')

    @classmethod
    def load(cls, root_directory, config_file_name):
        """Read and validate the config file into a Configuration object
        """
        if not os.path.isdir(root_directory):
            raise BuildException(f'Root directory {root_directory} not found.')

        config_path = cls.build_config_file_path(root_directory, config_file_name)

        with open(config_path, 'r') as config_file:
            try:
                # OrderedDict ensures things are loaded in correct order
                data = json.load(config_file, object_pairs_hook=OrderedDict)
            except json.decoder.JSONDecodeError:
                raise BuildException(f'Error parsing JSON file at "{config_path}".')

            ensurer = KeyEnsurer(data=data, required_keys=cls.required_keys)
            if not ensurer.validate():
                separator = "\n  - "
                raise BuildException(
                    f'Missing required keys: {separator + separator.join(ensurer.missing)}'
                )

        return Configuration(
            root_directory=root_directory,
            config_file_path=config_file,
            template_paths=data['templates'],
            variable_paths=data.get('variables', [])
        )


class VariableManager:
    """Loads and manages the pulling of variables from YAML files
    """

    def __init__(self, variable_paths, variable_root):
        self.variable_paths = variable_paths
        self.variable_root = variable_root
        self.variable_data = OrderedDict()
        self._load_variable_files()
        self._create_variables()

    def _load_variable_files(self):
        """Load each variable YAML file into a dict
        """
        for path in self.variable_paths:
            with open(self._build_path(path), 'r') as variable_file:
                yaml_data = yaml.load(variable_file)
            self.variable_data[path] = yaml_data

    def _create_variables(self):
        """Using the loaded YAML data, deeply merge/flatten the variables
        """
        self.variables = {}
        for _, variables in reversed(self.variable_data.items()):
            if variables:
                self.variables = _merge_dicts(variables, self.variables)

    def _build_path(self, path):
        """Easy access to a specific variable path
        """
        return os.path.join(self.variable_root, path)


class TemplateManager:
    """Loads and manages the pulling and rendering of variables from YAML files
    """

    def __init__(self, variable_manager, template_paths, template_root):
        self.variables = variable_manager.variables
        self.template_paths = template_paths
        self.template_root = template_root
        self.template_data = OrderedDict()
        self._load_template_files_with_variables()

    def _load_template_files_with_variables(self):
        """
        Load each of the template files and render them with Jinja
        using the variable files.
        """
        jinja_env = Environment(
            loader=FileSystemLoader(self.template_root),
            trim_blocks=True,
            lstrip_blocks=True
        )
        for path in self.template_paths:
            template = jinja_env.get_template(path)
            yaml_string = template.render(self.variables)
            self.template_data[path] = yaml.load(yaml_string)

    def merge_template_data(self):
        """Merge each rendered template into one final template
        """
        template = {}
        for _, cur_template in reversed(self.template_data.items()):
            if cur_template:
                template = _merge_dicts(cur_template, template)
        return template



class PackerTemplateMerger:
    """Application instance that controls the flow of building the template
    """

    config = None

    template_manager = None
    variable_manager = None

    def __init__(self, options, configuration=None):
        """Load each manager to prepare for assembly
        """
        self.options = options
        if not configuration:
            configuration = Configuration.load(
                root_directory=options.directory,
                config_file_name=options.config_path
            )
        self.config = configuration
        self.variable_manager = VariableManager(
            variable_paths=self.config.variable_paths,
            variable_root=self.config.root_directory
        )
        self.template_manager = TemplateManager(
            variable_manager=self.variable_manager,
            template_paths=self.config.template_paths,
            template_root=self.config.root_directory
        )


    def assemble_template(self):
        """The core functionality that builds the template

        Builds the template, and writes it to either a
        temp file or to an output file given from command line.
        """
        template = self.template_manager.merge_template_data()
        if self.options.out_file and not self.options.dry_run:
            manifest_file = self.options.out_file
            out_file = open(manifest_file, 'w+')
        else:
            out_file = NamedTemporaryFile(delete=False, mode='w')
            manifest_file = out_file.name

        # When wrapped with context below, the file was unreachable
        # later on in the validation/build periods.
        json.dump(
            template,
            out_file,
            sort_keys=True,
            indent=4
        )
        out_file.close()

        if self._validate_template(manifest_file) and not self.options.dry_run:
            self._build_template(manifest_file)

        if self.options.dry_run:
            self._dry_run(template)

        # Unlink (Delete) the temporary file
        if not self.options.out_file:
            os.unlink(manifest_file)

    # pylint: disable=no-self-use
    def _divider(self, length=70):
        """Prints a pretty divider -----------
        """
        print('-' * length)

    def _dry_run(self, template):
        """Output the manifest to the console
        """
        self._divider()
        json_manifest = json.dumps(template, sort_keys=True, indent=4)
        print(json_manifest)
        exit(0)

    def _validate_template(self, manifest_file):
        """Run `packer validate` on a manifest_file path
        """
        process = subprocess.Popen(["packer", "validate", manifest_file], stdout=subprocess.PIPE)
        output = process.communicate()[0].decode('utf-8')
        if 'error' in output:
            self._divider()
            print('-- Error validating template --')
            for error in output.split('*')[1:]:
                print(f'* {error}')
            return False

        print('-- Template Passed Validation --')
        return True

    def _build_template(self, manifest_file):
        """Run `packer build` on a manifest_file path
        """
        process = subprocess.Popen(["packer", "build", manifest_file], stdout=subprocess.PIPE)
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                print(output.strip().decode('utf-8'))
            process.poll()



def main():
    """Setup and run the merger after figuring out command line arguments
    """
    command_line_args = parse_arguments()
    merger = PackerTemplateMerger(command_line_args)
    merger.assemble_template()

if __name__ == '__main__':
    main()
