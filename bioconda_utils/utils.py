#!/usr/bin/env python

import os
import glob
import subprocess as sp
import argparse
import itertools
import sys
import shutil
import contextlib
from collections import defaultdict, Iterable
from itertools import product, chain
import logging
import pkg_resources
import networkx as nx
import requests
from jsonschema import validate
import datetime

from conda_build import api
from conda_build.metadata import MetaData
import yaml

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def temp_env(env):
    """
    Context manager to temporarily set os.environ.

    Used to send values in `env` to processes that only read the os.environ,
    for example when filling in meta.yaml with jinja2 template variables.

    All values are converted to string before sending to os.environ
    """
    env = dict(env)
    orig = os.environ.copy()
    _env = {k: str(v) for k, v in env.items()}
    os.environ.update(_env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(orig)


def run(cmds, env=None):
    """
    Wrapper around subprocess.run()

    Explicitly decodes stdout to avoid UnicodeDecodeErrors that can occur when
    using the `universal_newlines=True` argument in the standard
    subprocess.run.

    Also uses check=True and merges stderr with stdout. If a CalledProcessError
    is raised, the output is decoded.

    Returns the subprocess.CompletedProcess object.
    """
    try:
        p = sp.run(cmds, stdout=sp.PIPE, stderr=sp.STDOUT, check=True, env=env)
        p.stdout = p.stdout.decode(errors='replace')
    except sp.CalledProcessError as e:
        e.stdout = e.stdout.decode(errors='replace')
        raise e
    return p


def envstr(env):
    env = dict(env)
    return ';'.join(['='.join([i, str(j)]) for i, j in sorted(env.items())])


def flatten_dict(dict):
    for key, values in dict.items():
        if isinstance(values, str) or not isinstance(values, Iterable):
            values = [values]
        yield [(key, value) for value in values]


class EnvMatrix:
    """
    Intended to be initialized with a YAML file and iterated over to yield all
    combinations of environments.

    YAML file has the following format::

        CONDA_PY:
          - "2.7"
          - "3.5"
        CONDA_BOOST: "1.60"
        CONDA_PERL: "5.22.0"
        CONDA_NPY: "110"
        CONDA_NCURSES: "5.9"
        CONDA_GSL: "1.16"

    """

    def __init__(self, env):
        """
        Parameters
        ----------

        env : str or dict
            If str, assume it's a path to a YAML-format filename and load it
            into a dict. If a dict is provided, use it directly.
        """
        if isinstance(env, str):
            with open(env) as f:
                self.env = yaml.load(f)
        else:
            self.env = env
        for key, val in self.env.items():
            if key != "CONDA_PY" and not isinstance(val, str):
                raise ValueError(
                    "All versions except CONDA_PY must be strings.")

    def __iter__(self):
        """
        Given the YAML::

            CONDA_PY:
              - "2.7"
              - "3.5"
            CONDA_BOOST: "1.60"
            CONDA_NPY: "110"

        We get the following sets of env vars::

          [('CONDA_BOOST', '1.60'), ('CONDA_PY', '2.7'), ('CONDA_NPY', '110')]
          [('CONDA_BOOST', '1.60'), ('CONDA_PY', '3.5'), ('CONDA_NPY', '110')]

        A copy of the entire os.environ dict is updated and yielded for each of
        these sets.
        """
        for env in product(*flatten_dict(self.env)):
            yield env


def get_deps(recipe, build=True):
    """
    Generator of dependencies for a single recipe

    Only names (not versions) of dependencies are yielded.

    Parameters
    ----------
    recipe : str or MetaData
        If string, it is a path to the recipe; otherwise assume it is a parsed
        conda_build.metadata.MetaData instance.

    build : bool
        If True yield build dependencies, if False yield run dependencies.
    """
    if isinstance(recipe, str):
        metadata = MetaData(recipe)
    else:
        metadata = recipe
    for dep in metadata.get_value(
            "requirements/{}".format("build" if build else "run"), []):
        yield dep.split()[0]


def get_dag(recipes, blacklist=None, restrict=True):
    """
    Returns the DAG of recipe paths and a dictionary that maps package names to
    lists of recipe paths to all defined versions of the package.  defined
    versions.

    Parameters
    ----------
    recipes : iterable
        An iterable of recipe paths, typically obtained via `get_recipes()`

    blacklist : set
        Package names to skip

    restrict : bool
        If True, then dependencies will be included in the DAG only if they are
        themselves in `recipes`. Otherwise, include all dependencies of
        `recipes`.

    Returns
    -------
    dag : nx.DiGraph
        Directed graph of packages -- nodes are package names; edges are
        dependencies (both run and build dependencies)

    name2recipe : dict
        Dictionary mapping package names to recipe paths. These recipe path
        values are lists and contain paths to all defined versions.
    """
    recipes = list(recipes)
    metadata = [MetaData(recipe) for recipe in recipes]
    if blacklist is None:
        blacklist = set()

    # meta.yaml's package:name mapped to the recipe path
    name2recipe = defaultdict(list)
    for meta, recipe in zip(metadata, recipes):
        name = meta.get_value('package/name')
        if name not in blacklist:
            name2recipe[name].append(recipe)

    def get_inner_deps(dependencies):
        for dep in dependencies:
            name = dep.split()[0]
            if name in name2recipe or not restrict:
                yield name

    dag = nx.DiGraph()
    dag.add_nodes_from(meta.get_value("package/name") for meta in metadata)
    for meta in metadata:
        name = meta.get_value("package/name")
        dag.add_edges_from((dep, name)
                           for dep in set(get_inner_deps(chain(
                               get_deps(meta),
                               get_deps(meta,
                                        build=False)))))

    return dag, name2recipe


def get_recipes(recipe_folder, package="*"):
    """
    Generator of recipes.

    Finds (possibly nested) directories containing a `meta.yaml` file.

    Parameters
    ----------
    recipe_folder : str
        Top-level dir of the recipes

    package : str or iterable
        Pattern or patterns to restrict the results.
    """
    if isinstance(package, str):
        package = [package]
    for p in package:
        logger.debug(
            "get_recipes(%s, package='%s'): %s", recipe_folder, package, p)
        path = os.path.join(recipe_folder, p)
        yield from map(os.path.dirname,
                       glob.glob(os.path.join(path, "meta.yaml")))
        yield from map(os.path.dirname,
                       glob.glob(os.path.join(path, "*", "meta.yaml")))


def get_channel_packages(channel='bioconda', platform=None):
    """
    Retrieves the existing packages for a channel from conda.anaconda.org

    Parameters
    ----------
    channel : str
        Channel to retrieve packages for

    platform : None | linux | osx
        Platform (OS) to retrieve packages for from `channel`. If None, use the
        currently-detected platform.
    """
    url_template = 'https://conda.anaconda.org/{channel}/{arch}/repodata.json'
    if (
        (platform == 'linux') or
        (platform is None and sys.platform.startswith("linux"))
    ):
        arch = 'linux-64'
    elif (
        (platform == 'osx') or
        (platform is None and sys.platform.startswith("darwin"))
    ):
        arch = 'osx-64'
    else:
        raise ValueError(
            'Unsupported OS: bioconda only supports linux and osx.')

    url = url_template.format(channel=channel, arch=arch)
    repodata = requests.get(url)
    if repodata.status_code != 200:
        raise requests.HTTPError(
            '{0.status_code} {0.reason} for {1}'
            .format(repodata, url))

    noarch_url = url_template.format(channel=channel, arch='noarch')
    noarch_repodata = requests.get(noarch_url)
    if noarch_repodata.status_code != 200:
        raise requests.HTTPError(
            '{0.status_code} {0.reason} for {1}'
            .format(noarch_repodata, noarch_url))

    channel_packages = set(repodata.json()['packages'].keys())
    channel_packages.update(noarch_repodata.json()['packages'].keys())
    return channel_packages


def _string_or_float_to_integer_python(s):
    """
    conda-build 2.0.4 expects CONDA_PY values to be integers (e.g., 27, 35) but
    older versions were OK with strings or even floats.

    To avoid editing existing config files, we support those values here.
    """

    try:
        s = float(s)
        if s < 10:  # it'll be a looong time before we hit Python 10.0
            s = int(s * 10)
        else:
            s = int(s)
    except ValueError:
        raise ValueError("{} is an unrecognized Python version".format(s))
    return s


def built_package_path(recipe, env=None):
    """
    Returns the path to which a recipe would be built.

    Does not necessarily exist; equivalent to `conda build --output recipename`
    but without the subprocess.
    """
    if env is None:
        env = {}
    env = dict(env)

    # Ensure CONDA_PY is an integer (needed by conda-build 2.0.4)
    py = env.get('CONDA_PY', None)
    env = dict(env)
    if py is not None:
        env['CONDA_PY'] = _string_or_float_to_integer_python(py)

    with temp_env(env):
        # Disabling set_build_id prevents the creation of uniquely-named work
        # directories just for checking the output file.
        # It needs to be done within the context manager so that it sees the
        # os.environ.
        config = api.Config(
            no_download_source=True,
            set_build_id=False)
        path = api.get_output_file_path(recipe, config=config)
    return path


class Target:
    def __init__(self, pkg, env):
        """
        Class to represent a package built with a particular environment
        (e.g. from EnvMatirix).
        """
        self.pkg = pkg
        self.env = env

    def __hash__(self):
        return self.pkg.__hash__()

    def __eq__(self, other):
        return self.pkg == other.pkg

    def __str__(self):
        return os.path.basename(self.pkg)

    def envstring(self):
        return ';'.join(['='.join([i, str(j)]) for i, j in self.env])


def last_commit_to_master():
    """
    Identifies the day of the last commit to master branch.
    """
    if not shutil.which('git'):
        raise ValueError("git not found")
    p = sp.run(
        'git log master --date=iso | grep "^Date:" | head -n1',
        shell=True, stdout=sp.PIPE, check=True
    )
    date = datetime.datetime.strptime(
        p.stdout[:-1].decode().split()[1],
        '%Y-%m-%d')
    return date


def filter_recipes(recipes, env_matrix, channels=None, force=False, quick=True):
    """
    Generator yielding only those recipes that should be built.

    Parameters
    ----------
    recipes : iterable
        Iterable of candidate recipes

    env_matrix : str, dict, or EnvMatrix
        If str or dict, create an EnvMatrix; if EnvMatrix already use it as-is.

    channels : None or list
        Optional list of channels to check for existing recipes

    force : bool
        Build the package even if it is already available in supplied channels.

    quick : bool
        If True, then if a recipe hasn't changed within two days after the last
        merge to the master branch, then skip it. This helps speed up testing.
    """
    if not isinstance(env_matrix, EnvMatrix):
        env_matrix = EnvMatrix(env_matrix)

    if channels is None:
        channels = []

    channel_packages = defaultdict(set)
    for channel in channels:
        channel_packages[channel].update(get_channel_packages(channel=channel))


    def tobuild(recipe, env):
        # TODO: get the modification time of recipe/meta.yaml. Only continue
        # the slow steps below if it's newer than the last commit to master.
        if force:
            logger.debug(
                'BIOCONDA FILTER: building %s because force=True', recipe)
            return True

        pkg = os.path.basename(built_package_path(recipe, env))
        in_channels = [
            channel for channel, pkgs in channel_packages.items()
            if pkg in pkgs
        ]
        if in_channels:
            logger.debug(
                'BIOCONDA FILTER: not building %s because '
                'it is in channel(s): %s', pkg, in_channels)
            return False

        with temp_env(env):
            # with temp_env, MetaData sees everything in env added to
            # os.environ.
            skip = MetaData(recipe).skip()

        if skip:
            logger.debug(
                'BIOCONDA FILTER: not building %s because '
                'it defines skip for this env', pkg)
            return False

        logger.debug(
            'BIOCONDA FILTER: building %s because it is not in channels '
            'does not define skip, and force is not specified', pkg)
        return True

    logger.debug('recipes: %s', recipes)
    recipes = list(recipes)
    nrecipes = len(recipes)

    if quick:
        last = last_commit_to_master()

        def is_new(recipe):
            for fn in os.listdir(recipe):
                m = datetime.datetime.fromtimestamp(
                    os.path.getmtime(os.path.join(recipe, fn))
                )
                diff = (m - last).days
                if diff > -2:
                    return True

        recipes = [recipe for recipe in recipes if is_new(recipe)]
        logger.info('Quick filter: filtered out %s of %s recipes '
                    'that are >2 days older than master branch',
                    nrecipes - len(recipes), nrecipes)
        nrecipes = len(recipes)
        if nrecipes == 0:
            raise StopIteration

    max_recipe = max(map(len, recipes))
    template = (
        'Filtering {{0}} of {{1}} ({{2:.1f}}%) {{3:<{0}}}'.format(max_recipe)
    )
    print(flush=True)
    try:
        for i, recipe in enumerate(sorted(recipes)):
            perc = (i + 1) / nrecipes * 100
            print(
                template.format(i + 1, nrecipes, perc, recipe),
                end='\r'
            )
            targets = set()
            for env in env_matrix:
                pkg = built_package_path(recipe, env)
                if tobuild(recipe, env):
                    targets.update([Target(pkg, env)])
            if targets:
                yield recipe, targets
    except sp.CalledProcessError as e:
        logger.debug(e.stdout)
        logger.error(e.stderr)
        exit(1)
    print(flush=True)


def get_blacklist(blacklists, recipe_folder):
    "Return list of recipes to skip from blacklists"
    blacklist = set()
    for p in blacklists:
        blacklist.update(
            [
                os.path.relpath(i.strip(), recipe_folder)
                for i in open(p) if not i.startswith('#') and i.strip()
            ]
        )
    return blacklist


def validate_config(config):
    """
    Validate config against schema

    Parameters
    ----------
    config : str or dict
        If str, assume it's a path to YAML file and load it. If dict, use it
        directly.
    """
    if not isinstance(config, dict):
        config = yaml.load(open(config))
    fn = pkg_resources.resource_filename(
        'bioconda_utils', 'config.schema.yaml'
    )
    schema = yaml.load(open(fn))
    validate(config, schema)


def load_config(path):
    validate_config(path)

    if isinstance(path, dict):
        config = path
        relpath = lambda p: p
    else:
        config = yaml.load(open(path))
        relpath = lambda p: os.path.relpath(p, os.path.dirname(path))

    def get_list(key):
        # always return empty list, also if NoneType is defined in yaml
        value = config.get(key)
        if value is None:
            return []
        return value

    default_config = {
        'env_matrix': {'CONDA_PY': 35},
        'blacklists': [],
        'channels': [],
        'docker_image': 'condaforge/linux-anvil',
        'requirements': None,
        'upload_channel': 'bioconda'
    }
    if 'env_matrix' in config:
        if isinstance(config['env_matrix'], str):
            config['env_matrix'] = relpath(config['env_matrix'])
    if 'blacklists' in config:
        config['blacklists'] = [relpath(p) for p in get_list('blacklists')]
    if 'channels' in config:
        config['channels'] = get_list('channels')

    default_config.update(config)
    return default_config
