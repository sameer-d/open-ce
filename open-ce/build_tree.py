"""
*****************************************************************
Licensed Materials - Property of IBM
(C) Copyright IBM Corp. 2020. All Rights Reserved.
US Government Users Restricted Rights - Use, duplication or
disclosure restricted by GSA ADP Schedule Contract with IBM Corp.
*****************************************************************
"""

import os
import utils
import env_config
import build_feedstock
from errors import OpenCEError, Error

import conda_build.api
from conda_build.config import get_or_merge_config

class BuildCommand():
    """
    The BuildCommand class holds all of the information needed to call the build_feedstock
    function a single time.
    """
    #pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(self,
                 recipe,
                 repository,
                 packages,
                 python=None,
                 build_type=None,
                 mpi_type=None,
                 run_dependencies=None,
                 host_dependencies=None,
                 build_dependencies=None,
                 test_dependencies=None,
                 channels=None,
                 build_command_dependencies=None):
        self.recipe = recipe
        self.repository = repository
        self.packages = packages
        self.python = python
        self.build_type = build_type
        self.mpi_type = mpi_type
        self.run_dependencies = run_dependencies
        self.host_dependencies = host_dependencies
        self.build_dependencies = build_dependencies
        self.test_dependencies = test_dependencies
        self.channels = channels
        self.build_command_dependencies = build_command_dependencies
        if self.build_command_dependencies is None:
            self.build_command_dependencies = []

    def feedstock_args(self):
        """
        Returns a list of strings that can be provided to the build_feedstock function to
        perform a build.
        """
        build_args = ["--working_directory", self.repository]

        for channel in self.channels:
            build_args += ["--channels", channel]

        build_args += ["--python_versions", self.python]
        build_args += ["--build_types", self.build_type]
        build_args += ["--mpi_types", self.mpi_type]

        if self.recipe:
            build_args += ["--recipes", self.recipe]

        return build_args

    def name(self):
        """
        Returns a name representing the Build Command
        """
        result = self.recipe
        variant_string = utils.variant_string(self.python, self.build_type, self.mpi_type)
        if variant_string:
            result += "-" + variant_string

        result = result.replace(".", "-")
        result = result.replace("_", "-")
        return result

def _make_hash(to_hash):
    '''Generic hash function.'''
    return hash(str(to_hash))

def _create_recipes(repository, recipes, variant_config_files, variants, channels):#pylint: disable=too-many-locals
    """
    Create a recipe dictionary for each recipe within a repository. The dictionary
    will have all of the information needed to build the recipe, as well as to
    create the dependency tree.
    """
    saved_working_directory = os.getcwd()
    os.chdir(repository)

    config_data, _ = build_feedstock.load_package_config()
    combined_config_files = variant_config_files

    feedstock_conda_build_config_file = build_feedstock.get_conda_build_config()
    if feedstock_conda_build_config_file:
        combined_config_files.append(feedstock_conda_build_config_file)
    outputs = []
    for recipe in config_data.get('recipes', []):
        if recipes and not recipe.get('name') in recipes:
            continue
        packages, run_deps, host_deps, build_deps, test_deps = _get_package_dependencies(recipe.get('path'),
                                                                                         combined_config_files,
                                                                                         variants)
        outputs.append(BuildCommand(recipe=recipe.get('name', None),
                                    repository=repository,
                                    packages=packages,
                                    python=variants['python'],
                                    build_type=variants['build_type'],
                                    mpi_type=variants['mpi_type'],
                                    run_dependencies=run_deps,
                                    host_dependencies=host_deps,
                                    build_dependencies=build_deps,
                                    test_dependencies=test_deps,
                                    channels=channels if channels else []))

    os.chdir(saved_working_directory)
    return outputs

def _get_package_dependencies(path, variant_config_files, variants):
    """
    Return a list of output packages and a list of dependency packages
    for the recipe at a given path. Uses conda-render to determine this information.
    """
    # Call conda-build's render tool to get a list of dictionaries representing
    # the recipe for each variant that will be built.
    config = get_or_merge_config(None)
    config.variant_config_files = variant_config_files
    config.verbose = False
    metas = conda_build.api.render(path,
                                   config=config,
                                   variants=variants,
                                   bypass_env_check=True,
                                   finalize=False)

    # Parse out the package names and dependencies from each variant
    packages = set()
    run_deps = set()
    host_deps = set()
    build_deps = set()
    test_deps = set()
    for meta,_,_ in metas:
        packages.add(meta.meta['package']['name'])
        run_deps.update(meta.meta['requirements'].get('run', []))
        host_deps.update(meta.meta['requirements'].get('host', []))
        build_deps.update(meta.meta['requirements'].get('build', []))
        if 'test' in meta.meta:
            test_deps.update(meta.meta['test'].get('requires', []))

    return packages, run_deps, host_deps, build_deps, test_deps

def _add_build_command_dependencies(build_commands, start_index=0):
    """
    Create a dependency tree for a list of build commands.

    Each build_command will contain a `build_command_dependencies` key which contains a list of integers.
    Each integer in the list represents the index of the dependencies build_commands within the
    list.

    The start_index indicates the value that the dependency indices should start
    counting from.
    """

    # Create a packages dictionary that uses all of a recipe's packages as key, with
    # the recipes index as values.
    packages = dict()
    for index, build_command in enumerate(build_commands):
        for package in build_command.packages:
            packages.update({ package : [start_index + index] + packages.get(package, []) })

    # Add a list of indices for dependency to a BuildCommand's `build_command_dependencies` value
    # Note: This will filter out all dependencies that aren't in the recipes list.
    for index, build_command in enumerate(build_commands):
        deps = []
        dependencies = set()
        dependencies.update({utils.remove_version(dep) for dep in build_command.run_dependencies})
        dependencies.update({utils.remove_version(dep) for dep in build_command.build_dependencies})
        dependencies.update({utils.remove_version(dep) for dep in build_command.host_dependencies})
        dependencies.update({utils.remove_version(dep) for dep in build_command.test_dependencies})
        for dep in dependencies:
            if dep in packages:
                deps += filter(lambda x: x != start_index + index, packages[dep])
        build_command.build_command_dependencies = deps

class BuildTree(): #pylint: disable=too-many-instance-attributes
    """
    An interable container of BuildCommands.

    Creating a BuildTree will:
    1. Clone all of the repositories listed in the provided `env_config_files`
       into the directory `repository_folder`.
    2. Build commands will be generated for each recipe listed in the provided
       `env_config_files` for each combination of python_versions and build_types.
    3. Dependency information will be added to each BuildCommand.

    Iterating over a BuildTree will always return BuildCommands before a BuildCommand
    that depends on it. Note: If there is a circular dependency within the provided
    recipes, infinite recursion can occur.
    """

    #pylint: disable=too-many-arguments
    def __init__(self,
                 env_config_files,
                 python_versions,
                 build_types,
                 mpi_types,
                 repository_folder="./",
                 git_location=utils.DEFAULT_GIT_LOCATION,
                 git_tag_for_env="master",
                 conda_build_config=utils.DEFAULT_CONDA_BUILD_CONFIG):

        self._env_config_files = env_config_files
        self._repository_folder = repository_folder
        self._git_location = git_location
        self._git_tag_for_env = git_tag_for_env
        self._conda_build_config = conda_build_config
        self._external_dependencies = dict()

        # Create a dependency tree that includes recipes for every combination
        # of variants.
        self._possible_variants = utils.make_variants(python_versions, build_types, mpi_types)
        self.build_commands = []
        for variant in self._possible_variants:
            try:
                variant_recipes, external_deps = self._create_all_recipes(variant)
            except OpenCEError as exc:
                raise OpenCEError(Error.CREATE_BUILD_TREE, exc.msg) from exc
            self._external_dependencies[str(variant)] = external_deps
            # Add dependency tree information to the packages list
            _add_build_command_dependencies(variant_recipes, len(self.build_commands))
            self.build_commands += variant_recipes
        self._detect_cycle()

    def _create_all_recipes(self, variants): #pylint: disable=too-many-branches
        '''
        Create a recipe dictionary for each recipe needed for a given environment file.
        '''

        env_config_data_list = env_config.load_env_config_files(self._env_config_files, variants)
        packages_seen = set()
        recipes = []
        external_deps = []
        # Create recipe dictionaries for each repository in the environment file
        for env_config_data in env_config_data_list:

            packages = env_config_data.get(env_config.Key.packages.name, [])
            if not packages:
                packages = []
            for package in packages:
                if _make_hash(package) in packages_seen:
                    continue

                # If the feedstock value starts with any of the SUPPORTED_GIT_PROTOCOLS, treat it as a url. Otherwise
                # combine with git_location and append "-feedstock.git"
                feedstock_value = package[env_config.Key.feedstock.name]
                if any(feedstock_value.startswith(protocol) for protocol in utils.SUPPORTED_GIT_PROTOCOLS):
                    git_url = feedstock_value
                    if not git_url.endswith(".git"):
                        git_url += ".git"
                    repository = os.path.splitext(os.path.basename(git_url))[0]
                else:
                    git_url = "{}/{}-feedstock.git".format(self._git_location, feedstock_value)

                    repository = feedstock_value + "-feedstock"

                # Check if the directory for the feedstock already exists.
                # If it doesn't attempt to clone the repository.
                if self._repository_folder:
                    repo_dir = os.path.join(self._repository_folder, repository)
                else:
                    repo_dir = repository

                if not os.path.exists(repo_dir):
                    self._clone_repo(git_url, repo_dir, env_config_data, package.get('git_tag'))

                recipes += _create_recipes(repo_dir,
                                           package.get('recipes'),
                                           [os.path.abspath(self._conda_build_config)],
                                           variants,
                                           env_config_data.get(env_config.Key.channels.name, None))
                packages_seen.add(_make_hash(package))

            current_deps = env_config_data.get(env_config.Key.external_dependencies.name, [])
            if current_deps:
                external_deps += current_deps

        return recipes, external_deps

    def _clone_repo(self, git_url, repo_dir, env_config_data, git_tag_from_config):
        """
        Clone the git repo at repository.
        """
        # Priority is given to command line specified tag, if it is not
        # specified then package specific tag, and when even that is not specified
        # then top level git tag specified for env in the env file. And if nothing is
        # at all specified then fall back to default branch of the repo.

        git_tag = self._git_tag_for_env
        if git_tag is None:
            if git_tag_from_config:
                git_tag = git_tag_from_config
            else:
                git_tag = env_config_data.get(env_config.Key.git_tag_for_env.name, None)

        if git_tag is None:
            clone_cmd = "git clone " + git_url + " " + repo_dir
        else:
            clone_cmd = "git clone -b " + git_tag + " --single-branch " + git_url + " " + repo_dir

        print("Clone cmd: ", clone_cmd)
        clone_result = os.system(clone_cmd)
        if clone_result != 0:
            raise OpenCEError(Error.CLONE_REPO, git_url)

    def __iter__(self):
        """
        Generator function that goes through every recipe in a list.
        If a recipe has dependencies, those recipes will be returned
        first.
        """
        is_seen = [False for i in range(len(self.build_commands))]
        yield from self._traverse_deps_tree(is_seen, range(len(self.build_commands)))

    def _traverse_deps_tree(self, is_seen, deps):
        """
        Generator function that goes through a recipes dependency tree.
        """
        for dep in deps:
            if is_seen[dep]:
                continue
            yield from self._traverse_deps_tree(is_seen, self.build_commands[dep].build_command_dependencies)
            yield self.build_commands[dep]
            is_seen[dep] = True

    def __getitem__(self, key):
        return self.build_commands[key]

    def __len__(self):
        return len(self.build_commands)

    def get_external_dependencies(self, variant):
        '''Return the list of external dependencies for the given variant.'''
        return self._external_dependencies.get(str(variant), [])

    def _detect_cycle(self, max_cycles=10):
        extract_build_tree = [x.build_command_dependencies for x in self.build_commands]
        cycles = []
        for start in range(len(self.build_commands)): # Check to see if there are any cycles that start anywhere in the tree.
            cycles += find_all_cycles(extract_build_tree, start)
            if len(cycles) >= max_cycles:
                break
        if cycles:
            cycle_print = "\n".join([" -> ".join([self.build_commands[i].recipe
                                                                    for i in cycle])
                                                                    for cycle in cycles[:min(max_cycles, len(cycles))]])
            if len(cycles) > max_cycles:
                cycle_print += "\nCycles truncated after {}...".format(max_cycles)
            raise OpenCEError(Error.BUILD_TREE_CYCLE, cycle_print)

def find_all_cycles(tree, current=0, seen=None):
    '''
    This function performs a depth first search of a tree from current, returning all cycles
    starting at current.
    '''
    if not seen:
        seen = []
    current_branch = seen + [current]
    if len(current_branch) != len(set(current_branch)):
        return [current_branch]
    result = []
    for dependency in tree[current]:
        next_step = find_all_cycles(tree, dependency, current_branch)
        if next_step:
            result += next_step
    return result
