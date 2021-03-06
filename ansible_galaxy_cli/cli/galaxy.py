########################################################################
#
# (C) 2018, Ansible by Red Hat
#
# This file is part of Mazer
#
# Mazer is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mazer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible. If not, see <http://www.gnu.org/licenses/>.
#
########################################################################

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import logging
import os
import sys

from ansible_galaxy_cli import cli
from ansible_galaxy_cli import __version__ as galaxy_cli_version
from ansible_galaxy.actions import build
from ansible_galaxy.actions import info
from ansible_galaxy.actions import install
from ansible_galaxy.actions import list as list_action
from ansible_galaxy.actions import remove
from ansible_galaxy.actions import version
from ansible_galaxy.config import defaults
from ansible_galaxy.config import config
from ansible_galaxy_cli import exceptions as cli_exceptions
from ansible_galaxy import matchers
from ansible_galaxy.models.context import GalaxyContext
from ansible_galaxy.models.build_context import BuildContext

from ansible_galaxy import rest_api

log = logging.getLogger(__name__)


def exit_without_ignore(ignore_errors, msg=None, rc=1):
    """
    Exits with the specified return code unless the
    option --ignore-errors was specified
    """
    ignore_error_blurb = '- you can use --ignore-errors to skip failed content items and finish processing the list.'
    if not ignore_errors:
        message = ignore_error_blurb
        if msg:
            message = '%s:\n%s' % (msg, ignore_error_blurb)
        raise cli_exceptions.GalaxyCliError(message)


def get_config_path_from_env():
    for env_var in ('MAZER_CONFIG', 'ANSIBLE_GALAXY_CONFIG'):
        raw_config_file_path = os.environ.get(env_var, None)

        if raw_config_file_path:
            log.info("Using config file '%s' as specified by env var '%s'",
                     raw_config_file_path, env_var)
            return raw_config_file_path

    return None


class GalaxyCLI(cli.CLI):
    SKIP_INFO_KEYS = ("name", "description", "readme_html", "related", "summary_fields", "average_aw_composite", "average_aw_score", "url")
    VALID_ACTIONS = ("build", "info", "install", "list", "remove", "version")
    VALID_ACTION_ALIASES = {'content-install': 'install'}

    def __init__(self, args):
        self.api = None
        super(GalaxyCLI, self).__init__(args)

    def set_action(self):

        super(GalaxyCLI, self).set_action()

        if self.action == "build":
            self.parser.set_usage("usage: %prog build [options]")
            self.parser.add_option('--collection-path', dest='collection_path', default=None,
                                   help='The path in which the collection repository is located. The default is the current working directory.')
            self.parser.add_option('--output-path', dest='output_path', default=None,
                                   help='The path in which the collection artifact will be created. The default is ./releases/.')

        if self.action == "info":
            self.parser.set_usage("usage: %prog info [options] repo_name[,version]")
        elif self.action == "install":
            self.parser.set_usage("usage: %prog install [options] [-r FILE | repo_name(s)[,version] | scm+repo_url[,version] | tar_file(s)]")
            self.parser.add_option('-g', '--global', dest='global_install', action='store_true',
                                   help='Install content to the path containing your global or system-wide content. The default is the '
                                   'global_content_path configured in your mazer.yml file (/usr/share/ansible/content, if not configured)')
            self.parser.add_option('-e', '--editable', dest='editable_install', action='store_true',
                                   help='Link a local directory into the content path for development and testing')
            self.parser.add_option('-i', '--ignore-errors', dest='ignore_errors', action='store_true', default=False,
                                   help='Ignore errors and continue with the next specified repo.')
            self.parser.add_option('-n', '--no-deps', dest='no_deps', action='store_true', default=False, help='Don\'t download roles listed as dependencies')
            self.parser.add_option('-r', '--role-file', dest='role_file', help='A file containing a list of roles to be imported')
            self.parser.add_option('--namespace', dest='namespace', default=None,
                                   help='The namespace to use when installing content (required for installs from local scm repo or archives)')
        elif self.action == "remove":
            self.parser.set_usage("usage: %prog remove repo1 repo2 ...")
        elif self.action == "list":
            self.parser.set_usage("usage: %prog list [repo_name]")
        elif self.action == "version":
            self.parser.set_usage("usage: %prog version")

        # options that apply to more than one action
        if self.action in ("info",):
            self.parser.add_option('--offline', dest='offline', default=False, action='store_true', help="Prevent Mazer from calling the galaxy API.")

        if self.action not in ("version",):
            # NOTE: while the option type=str, the default is a list, and the
            # callback will set the value to a list.
            self.parser.add_option('-C', '--content-path', dest='content_path',
                                   help='The path to the directory containing your Galaxy content. The default is the content_path configured in your'
                                        'mazer.yml file (~/.ansible/content, if not configured)', type='str')

        if self.action in ("install",):
            self.parser.add_option('-f', '--force', dest='force', action='store_true', default=False, help='Force overwriting an existing collection')

    def parse(self):
        ''' create an options parser for bin/ansible '''

        self.parser = cli.CLI.base_parser(
            usage="usage: %%prog [%s] [--help] [options] ..." % "|".join(self.VALID_ACTIONS),
            epilog="\nSee '%s <command> --help' for more information on a specific command.\n\n" % os.path.basename(sys.argv[0])
        )

        # common
        self.parser.add_option('-s', '--server', dest='server_url', default=None, help='The API server destination')

        self.parser.add_option('-c', '--ignore-certs', action='store_true', dest='ignore_certs', default=None,
                               help='Ignore SSL certificate validation errors.')
        self.set_action()

        super(GalaxyCLI, self).parse()

        if self.action == 'install' and getattr(self.options, 'content_path') and getattr(self.options, 'global_install'):
            raise cli_exceptions.CliOptionsError('--content-path and --global are mutually exclusive')

    def _get_galaxy_context(self, options, config):
        # use content_path from options if availble but fallback to configured content_path
        options_content_path = None
        if hasattr(options, 'content_path'):
            options_content_path = options.content_path

        raw_content_path = options_content_path or config.content_path

        if hasattr(options, 'global_install') and options.global_install:
            raw_content_path = config.global_content_path

        content_path = os.path.expanduser(raw_content_path)

        server = config.server.copy()

        if getattr(options, 'server_url', None):
            server['url'] = options.server_url

        if getattr(options, 'ignore_certs', None):
            # use ignore certs from options if available, but fallback to configured ignore_certs
            server['ignore_certs'] = options.ignore_certs

        galaxy_context = GalaxyContext(server=server, content_path=content_path)

        return galaxy_context

    def run(self):

        raw_config_file_path = get_config_path_from_env() or defaults.CONFIG_FILE

        self.config_file_path = os.path.abspath(os.path.expanduser(raw_config_file_path))

        super(GalaxyCLI, self).run()

        self.config = config.load(self.config_file_path)

        log.debug('configuration: %s', json.dumps(self.config.as_dict(), indent=None))

        # cli --server value or the url field of the first server in config
        # TODO: pass list of server config objects to GalaxyContext and/or create a GalaxyContext later
        # server_url = self.options.server_url or self.config['servers'][0]['url']
        # ignore_certs = self.options.ignore_certs or self.config['servers'][0]['ignore_certs']

        galaxy_context = self._get_galaxy_context(self.options, self.config)

        log.debug('galaxy context: %s', galaxy_context)

        self.api = rest_api.GalaxyAPI(galaxy_context)

        log.debug('execute action: %s', self.action)
        log.debug('execute action with options: %s', self.options)
        log.debug('execute action with args: %s', self.args)

        return self.execute()

    def execute_build(self):
        """
        Create a collection artifact from a collection src repository.
        """

        log.debug('options: %s', self.options)
        log.debug('args: %s', self.args)

        galaxy_context = self._get_galaxy_context(self.options, self.config)

        # default to looking for collection in cwd if not specified
        cwd = os.getcwd()
        collection_path = self.options.collection_path or cwd
        # write the output archive to output_path, which will default to collection
        # relative releases/
        output_path = self.options.output_path or os.path.join(collection_path, 'releases')

        build_context = BuildContext(collection_path=collection_path,
                                     output_path=output_path)

        return build.build(galaxy_context,
                           build_context,
                           display_callback=self.display)

    def execute_info(self):
        """
        Display detailed information about an installed collection, as well as info available from the Galaxy API.
        """

        if len(self.args) == 0:
            # the user needs to specify a role
            raise cli_exceptions.CliOptionsError("- you must specify a user/role name")

        log.debug('args=%s', self.args)

        galaxy_context = self._get_galaxy_context(self.options, self.config)

        content_specs = self.args

        # FIXME: rc?
        return info.info_content_specs(galaxy_context, self.api, content_specs,
                                       display_callback=self.display,
                                       offline=self.options.offline)

    def execute_install(self):
        """
        Install a collection.
        """

        install_content_type = 'all'

        self.log.debug('self.options: %s', self.options)
        galaxy_context = self._get_galaxy_context(self.options, self.config)
        requested_content_specs = self.args

        try:
            rc = install.install_content_specs(galaxy_context,
                                               editable=self.options.editable_install,
                                               content_spec_strings=requested_content_specs,
                                               install_content_type=install_content_type,
                                               namespace_override=self.options.namespace,
                                               display_callback=self.display,
                                               ignore_errors=self.options.ignore_errors,
                                               no_deps=self.options.no_deps,
                                               force_overwrite=self.options.force)
        except Exception as e:
            log.exception(e)
            raise

        return rc

    def execute_remove(self):
        """
        Remove a list of collections from the local system.
        """

        if len(self.args) == 0:
            raise cli_exceptions.CliOptionsError('- you must specify at least one role to remove.')

        galaxy_context = self._get_galaxy_context(self.options, self.config)

        if self.args:
            match_filter = matchers.MatchLabels(self.args)

        return remove.remove(galaxy_context,
                             repository_match_filter=match_filter,
                             display_callback=self.display)

    def execute_list(self):
        """
        List roles installed on the local file system.
        """

        galaxy_context = self._get_galaxy_context(self.options, self.config)

        match_filter = matchers.MatchAll()

        if self.args:
            match_filter = matchers.MatchNamespacesOrLabels(self.args)

        return list_action.list(galaxy_context,
                                repository_match_filter=match_filter,
                                display_callback=self.display)

    def execute_version(self):
        return version.version(config_file_path=self.config_file_path,
                               cli_version=galaxy_cli_version,
                               display_callback=self.display)
