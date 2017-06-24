#!/usr/bin/python

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import print_function

import os
import random
import re
import shlex
import tempfile
import uuid
import subprocess as sub
import json

import sys

try:
    # python 3
    from urllib.parse import quote_plus
except ImportError:
    # python 2
    from urllib import quote_plus
try:
    # python 3
    import configparser
except ImportError:
    # python 2
    import ConfigParser as configparser

def is_windows():
    return sys.platform.startswith('win')

def identity(x):
    return x

def cygpath(x):
    command = ["cygpath", "-wp", x]
    p = sub.Popen(command,stdout=sub.PIPE)
    output, errors = p.communicate()
    lines = output.split(os.linesep)
    return lines[0]

def init_storm_env():
    global CLUSTER_CONF_DIR
    ini_file = os.path.join(CLUSTER_CONF_DIR, 'storm_env.ini')
    if not os.path.isfile(ini_file):
        return
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(ini_file)
    options = config.options('environment')
    for option in options:
        value = config.get('environment', option)
        os.environ[option] = value

normclasspath = cygpath if sys.platform == 'cygwin' else identity
STORM_DIR = os.sep.join(os.path.realpath( __file__ ).split(os.sep)[:-2])
USER_CONF_DIR = os.path.expanduser("~" + os.sep + ".storm")
STORM_CONF_DIR = os.getenv('STORM_CONF_DIR', None)

if STORM_CONF_DIR == None:
    CLUSTER_CONF_DIR = os.path.join(STORM_DIR, "conf")
else:
    CLUSTER_CONF_DIR = STORM_CONF_DIR

if (not os.path.isfile(os.path.join(USER_CONF_DIR, "storm.yaml"))):
    USER_CONF_DIR = CLUSTER_CONF_DIR

STORM_LIB_DIR = os.path.join(STORM_DIR, "lib")
STORM_BIN_DIR = os.path.join(STORM_DIR, "bin")
STORM_LOG4J2_CONF_DIR = os.path.join(STORM_DIR, "log4j2")
STORM_SUPERVISOR_LOG_FILE = os.getenv('STORM_SUPERVISOR_LOG_FILE', "supervisor.log")

init_storm_env()

CONFIG_OPTS = []
CONFFILE = ""
JAR_JVM_OPTS = shlex.split(os.getenv('STORM_JAR_JVM_OPTS', ''))
JAVA_HOME = os.getenv('JAVA_HOME', None)
JAVA_CMD = 'java' if not JAVA_HOME else os.path.join(JAVA_HOME, 'bin', 'java')
if JAVA_HOME and not os.path.exists(JAVA_CMD):
    print("ERROR:  JAVA_HOME is invalid.  Could not find bin/java at %s." % JAVA_HOME)
    sys.exit(1)
STORM_EXT_CLASSPATH = os.getenv('STORM_EXT_CLASSPATH', None)
STORM_EXT_CLASSPATH_DAEMON = os.getenv('STORM_EXT_CLASSPATH_DAEMON', None)
DEP_JARS_OPTS = []
DEP_ARTIFACTS_OPTS = []
DEP_ARTIFACTS_REPOSITORIES_OPTS = []
DEP_PROXY_URL = None
DEP_PROXY_USERNAME = None
DEP_PROXY_PASSWORD = None

def get_config_opts():
    global CONFIG_OPTS
    return "-Dstorm.options=" + ','.join(map(quote_plus,CONFIG_OPTS))

if not os.path.exists(STORM_LIB_DIR):
    print("******************************************")
    print("The storm client can only be run from within a release. You appear to be trying to run the client from a checkout of Storm's source code.")
    print("\nYou can download a Storm release at http://storm.apache.org/downloads.html")
    print("******************************************")
    sys.exit(1)

def get_jars_full(adir):
    files = []
    if os.path.isdir(adir):
        files = os.listdir(adir)
    elif os.path.exists(adir):
        files = [adir]

    ret = []
    for f in files:
        if f.endswith(".jar"):
            ret.append(os.path.join(adir, f))
    return ret

def get_classpath(extrajars, daemon=True):
    ret = get_jars_full(STORM_DIR)
    ret.extend(get_jars_full(STORM_DIR + "/lib"))
    ret.extend(get_jars_full(STORM_DIR + "/extlib"))
    if daemon:
        ret.extend(get_jars_full(STORM_DIR + "/extlib-daemon"))
    if STORM_EXT_CLASSPATH != None:
        for path in STORM_EXT_CLASSPATH.split(os.pathsep):
            ret.extend(get_jars_full(path))
    if daemon and STORM_EXT_CLASSPATH_DAEMON != None:
        for path in STORM_EXT_CLASSPATH_DAEMON.split(os.pathsep):
            ret.extend(get_jars_full(path))
    ret.extend(extrajars)
    return normclasspath(os.pathsep.join(ret))

def confvalue(name, extrapaths, daemon=True):
    global CONFFILE
    command = [
        JAVA_CMD, get_config_opts(), "-Dstorm.conf.file=" + CONFFILE,
        "-cp", get_classpath(extrapaths, daemon), "org.apache.storm.command.config_value", name
    ]
    p = sub.Popen(command, stdout=sub.PIPE)
    output, errors = p.communicate()
    # python 3
    if not isinstance(output, str):
        output = output.decode('utf-8')
    lines = output.split(os.linesep)
    for line in lines:
        tokens = line.split(" ")
        if tokens[0] == "VALUE:":
            return " ".join(tokens[1:])
    return ""

def resolve_dependencies(artifacts, artifact_repositories, proxy_url, proxy_username, proxy_password):
    if len(artifacts) == 0:
        return {}

    print("Resolving dependencies on demand: artifacts (%s) with repositories (%s)" % (artifacts, artifact_repositories))

    if proxy_url is not None:
        print("Proxy information: url (%s) username (%s)" % (proxy_url, proxy_username))

    sys.stdout.flush()

    # TODO: should we move some external modules to outer place?

    # storm-submit module doesn't rely on storm-core and relevant libs
    extrajars = get_jars_full(STORM_DIR + "/external/storm-submit-tools")
    classpath = normclasspath(os.pathsep.join(extrajars))

    command = [
        JAVA_CMD, "-cp", classpath, "org.apache.storm.submit.command.DependencyResolverMain"
    ]

    command.extend(["--artifacts", ",".join(artifacts)])
    command.extend(["--artifactRepositories", ",".join(artifact_repositories)])

    if proxy_url is not None:
        command.extend(["--proxyUrl", proxy_url])
        if proxy_username is not None:
            command.extend(["--proxyUsername", proxy_username])
            command.extend(["--proxyPassword", proxy_password])

    p = sub.Popen(command, stdout=sub.PIPE)
    output, errors = p.communicate()
    if p.returncode != 0:
        raise RuntimeError("dependency handler returns non-zero code: code<%s> syserr<%s>" % (p.returncode, errors))

    # python 3
    if not isinstance(output, str):
        output = output.decode('utf-8')

    # For debug purpose, uncomment when you need to debug DependencyResolver
    #print("Resolved dependencies: %s" % output)

    try:
        out_dict = json.loads(output)
        return out_dict
    except:
        raise RuntimeError("dependency handler returns non-json response: sysout<%s>", output)

def print_localconfvalue(name):
    """Syntax: [storm localconfvalue conf-name]
    Prints out the value for conf-name in the local Storm configs.
    The local Storm configs are the ones in ~/.storm/storm.yaml merged
    in with the configs in defaults.yaml.
    """
    print(name + ": " + confvalue(name, [USER_CONF_DIR]))

def print_remoteconfvalue(name):
    """Syntax: [storm remoteconfvalue conf-name]
    Prints out the value for conf-name in the cluster's Storm configs.
    The cluster's Storm configs are the ones in $STORM-PATH/conf/storm.yaml
    merged in with the configs in defaults.yaml.
    This command must be run on a cluster machine.
    """
    print(name + ": " + confvalue(name, [CLUSTER_CONF_DIR]))

def parse_args(string):
    """Takes a string of whitespace-separated tokens and parses it into a list.
    Whitespace inside tokens may be quoted with single quotes, double quotes or
    backslash (similar to command-line arguments in bash).
    >>> parse_args(r'''"a a" 'b b' c\ c "d'd" 'e"e' 'f\'f' "g\"g" "i""i" 'j''j' k" "k l' l' mm n\\n''')
    ['a a', 'b b', 'c c', "d'd", 'e"e', "f'f", 'g"g', 'ii', 'jj', 'k k', 'l l', 'mm', r'n\n']
    """
    re_split = re.compile(r'''((?:
        [^\s"'\\] |
        "(?: [^"\\] | \\.)*" |
        '(?: [^'\\] | \\.)*' |
        \\.
    )+)''', re.VERBOSE)
    args = re_split.split(string)[1::2]
    args = [re.compile(r'"((?:[^"\\]|\\.)*)"').sub('\\1', x) for x in args]
    args = [re.compile(r"'((?:[^'\\]|\\.)*)'").sub('\\1', x) for x in args]
    return [re.compile(r'\\(.)').sub('\\1', x) for x in args]

def exec_storm_class(klass, jvmtype="-server", jvmopts=[], extrajars=[], args=[], fork=False, daemon=True, daemonName=""):
    global CONFFILE
    storm_log_dir = confvalue("storm.log.dir",[CLUSTER_CONF_DIR])
    if(storm_log_dir == None or storm_log_dir == "nil"):
        storm_log_dir = os.path.join(STORM_DIR, "logs")
    all_args = [ JAVA_CMD ]
    if jvmtype:
        all_args += [jvmtype]
    all_args += ["-Ddaemon.name=" + daemonName,
        get_config_opts(),
        "-Dstorm.home=" + STORM_DIR,
        "-Dstorm.log.dir=" + storm_log_dir,
        "-Djava.library.path=" + confvalue("java.library.path", extrajars, daemon),
        "-Dstorm.conf.file=" + CONFFILE,
        "-cp", get_classpath(extrajars, daemon),
    ] + jvmopts + [klass] + list(args)
    print("Running: " + " ".join(all_args))
    sys.stdout.flush()
    exit_code = 0
    if fork:
        exit_code = os.spawnvp(os.P_WAIT, JAVA_CMD, all_args)
    elif is_windows():
        # handling whitespaces in JAVA_CMD
        try:
            ret = sub.check_output(all_args, stderr=sub.STDOUT)
            print(ret)
        except sub.CalledProcessError as e:
            sys.exit(e.returncode)
    else:
        os.execvp(JAVA_CMD, all_args)
    return exit_code

def jar(jarfile, klass, *args):
    """Syntax: [storm jar topology-jar-path class ...]
    Runs the main method of class with the specified arguments.
    The storm jars and configs in ~/.storm are put on the classpath.
    The process is configured so that StormSubmitter
    (http://storm.apache.org/releases/current/javadocs/org/apache/storm/StormSubmitter.html)
    will upload the jar at topology-jar-path when the topology is submitted.
    When you want to ship other jars which is not included to application jar, you can pass them to --jars option with comma-separated string.
    For example, --jars "your-local-jar.jar,your-local-jar2.jar" will load your-local-jar.jar and your-local-jar2.jar.
    And when you want to ship maven artifacts and its transitive dependencies, you can pass them to --artifacts with comma-separated string.
    You can also exclude some dependencies like what you're doing in maven pom.
    Please add exclusion artifacts with '^' separated string after the artifact.
    For example, --artifacts "redis.clients:jedis:2.9.0,org.apache.kafka:kafka_2.10:0.8.2.2^org.slf4j:slf4j-log4j12" will load jedis and kafka artifact and all of transitive dependencies but exclude slf4j-log4j12 from kafka.
    When you need to pull the artifacts from other than Maven Central, you can pass remote repositories to --artifactRepositories option with comma-separated string.
    Repository format is "<name>^<url>". '^' is taken as separator because URL allows various characters.
    For example, --artifactRepositories "jboss-repository^http://repository.jboss.com/maven2,HDPRepo^http://repo.hortonworks.com/content/groups/public/" will add JBoss and HDP repositories for dependency resolver.
    You can also provide proxy information to let dependency resolver utilizing proxy if needed. There're three parameters for proxy:
    --proxyUrl: URL representation of proxy ('http://host:port')
    --proxyUsername: username of proxy if it requires basic auth
    --proxyPassword: password of proxy if it requires basic auth
    Complete example of options is here: `./bin/storm jar example/storm-starter/storm-starter-topologies-*.jar org.apache.storm.starter.RollingTopWords blobstore-remote2 remote --jars "./external/storm-redis/storm-redis-1.1.0.jar,./external/storm-kafka/storm-kafka-1.1.0.jar" --artifacts "redis.clients:jedis:2.9.0,org.apache.kafka:kafka_2.10:0.8.2.2^org.slf4j:slf4j-log4j12" --artifactRepositories "jboss-repository^http://repository.jboss.com/maven2,HDPRepo^http://repo.hortonworks.com/content/groups/public/"`
    When you pass jars and/or artifacts options, StormSubmitter will upload them when the topology is submitted, and they will be included to classpath of both the process which runs the class, and also workers for that topology.
    """
    global DEP_JARS_OPTS, DEP_ARTIFACTS_OPTS, DEP_ARTIFACTS_REPOSITORIES_OPTS, DEP_PROXY_URL, DEP_PROXY_USERNAME, DEP_PROXY_PASSWORD

    local_jars = DEP_JARS_OPTS
    artifact_to_file_jars = resolve_dependencies(DEP_ARTIFACTS_OPTS, DEP_ARTIFACTS_REPOSITORIES_OPTS, DEP_PROXY_URL, DEP_PROXY_USERNAME, DEP_PROXY_PASSWORD)

    transform_class = confvalue("client.jartransformer.class", [CLUSTER_CONF_DIR])
    if (transform_class != None and transform_class != "nil"):
        tmpjar = os.path.join(tempfile.gettempdir(), uuid.uuid1().hex+".jar")
        exec_storm_class("org.apache.storm.daemon.ClientJarTransformerRunner", args=[transform_class, jarfile, tmpjar], fork=True, daemon=False)
        extra_jars = [tmpjar, USER_CONF_DIR, STORM_BIN_DIR]
        extra_jars.extend(local_jars)
        extra_jars.extend(artifact_to_file_jars.values())
        topology_runner_exit_code = exec_storm_class(
                klass,
                jvmtype="",
                extrajars=extra_jars,
                args=args,
                daemon=False,
                fork=True,
                jvmopts=JAR_JVM_OPTS + ["-Dstorm.jar=" + tmpjar] +
                        ["-Dstorm.dependency.jars=" + ",".join(local_jars)] +
                        ["-Dstorm.dependency.artifacts=" + json.dumps(artifact_to_file_jars)])
        os.remove(tmpjar)
        sys.exit(topology_runner_exit_code)
    else:
        extra_jars=[jarfile, USER_CONF_DIR, STORM_BIN_DIR]
        extra_jars.extend(local_jars)
        extra_jars.extend(artifact_to_file_jars.values())
        exec_storm_class(
            klass,
            jvmtype="",
            extrajars=extra_jars,
            args=args,
            daemon=False,
            jvmopts=JAR_JVM_OPTS + ["-Dstorm.jar=" + jarfile] +
                    ["-Dstorm.dependency.jars=" + ",".join(local_jars)] +
                    ["-Dstorm.dependency.artifacts=" + json.dumps(artifact_to_file_jars)])

def sql(sql_file, topology_name):
    """Syntax: [storm sql sql-file topology-name], or [storm sql sql-file --explain] when activating explain mode
    Compiles the SQL statements into a Trident topology and submits it to Storm.
    If user activates explain mode, SQL Runner analyzes each query statement and shows query plan instead of submitting topology.
    --jars and --artifacts, and --artifactRepositories, --proxyUrl, --proxyUsername, --proxyPassword options available for jar are also applied to sql command.
    Please refer "help jar" to see how to use --jars and --artifacts, and --artifactRepositories, --proxyUrl, --proxyUsername, --proxyPassword options.
    You normally want to pass these options since you need to set data source to your sql which is an external storage in many cases.
    """
    global DEP_JARS_OPTS, DEP_ARTIFACTS_OPTS, DEP_ARTIFACTS_REPOSITORIES_OPTS, DEP_PROXY_URL, DEP_PROXY_USERNAME, DEP_PROXY_PASSWORD

    local_jars = DEP_JARS_OPTS
    artifact_to_file_jars = resolve_dependencies(DEP_ARTIFACTS_OPTS, DEP_ARTIFACTS_REPOSITORIES_OPTS, DEP_PROXY_URL, DEP_PROXY_USERNAME, DEP_PROXY_PASSWORD)

    sql_core_jars = get_jars_full(STORM_DIR + "/external/sql/storm-sql-core")
    sql_runtime_jars = get_jars_full(STORM_DIR + "/external/sql/storm-sql-runtime")

    # include storm-sql-runtime jar(s) to local jar list
    local_jars.extend(sql_runtime_jars)

    extrajars=[USER_CONF_DIR, STORM_BIN_DIR]
    extrajars.extend(local_jars)
    extrajars.extend(artifact_to_file_jars.values())

    # include this for running StormSqlRunner, but not for generated topology
    extrajars.extend(sql_core_jars)

    if topology_name == "--explain":
        args = ["--file", sql_file, "--explain"]
    else:
        args = ["--file", sql_file, "--topology", topology_name]

    exec_storm_class(
        "org.apache.storm.sql.StormSqlRunner",
        jvmtype="",
        extrajars=extrajars,
        args=args,
        daemon=False,
        jvmopts=["-Dstorm.dependency.jars=" + ",".join(local_jars)] +
                ["-Dstorm.dependency.artifacts=" + json.dumps(artifact_to_file_jars)])

def kill(*args):
    """Syntax: [storm kill topology-name [-w wait-time-secs]]
    Kills the topology with the name topology-name. Storm will
    first deactivate the topology's spouts for the duration of
    the topology's message timeout to allow all messages currently
    being processed to finish processing. Storm will then shutdown
    the workers and clean up their state. You can override the length
    of time Storm waits between deactivation and shutdown with the -w flag.
    """
    if not args:
        print_usage(command="kill")
        sys.exit(2)
    exec_storm_class(
        "org.apache.storm.command.kill_topology",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])


def upload_credentials(*args):
    """Syntax: [storm upload_credentials topology-name [credkey credvalue]*]
    Uploads a new set of credentials to a running topology
    """
    if not args:
        print_usage(command="upload_credentials")
        sys.exit(2)
    exec_storm_class(
        "org.apache.storm.command.upload_credentials",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def blobstore(*args):
    """Syntax: [storm blobstore cmd]
    list [KEY...] - lists blobs currently in the blob store
    cat [-f FILE] KEY - read a blob and then either write it to a file, or STDOUT (requires read access).
    create [-f FILE] [-a ACL ...] [--replication-factor NUMBER] KEY - create a new blob. Contents comes from a FILE
         or STDIN. ACL is in the form [uo]:[username]:[r-][w-][a-] can be comma separated list.
    update [-f FILE] KEY - update the contents of a blob.  Contents comes from
         a FILE or STDIN (requires write access).
    delete KEY - delete an entry from the blob store (requires write access).
    set-acl [-s ACL] KEY - ACL is in the form [uo]:[username]:[r-][w-][a-] can be comma
         separated list (requires admin access).
    replication --read KEY - Used to read the replication factor of the blob.
    replication --update --replication-factor NUMBER KEY where NUMBER > 0. It is used to update the
        replication factor of a blob.
    For example, the following would create a mytopo:data.tgz key using the data
    stored in data.tgz.  User alice would have full access, bob would have
    read/write access and everyone else would have read access.
    storm blobstore create mytopo:data.tgz -f data.tgz -a u:alice:rwa,u:bob:rw,o::r
    """
    exec_storm_class(
        "org.apache.storm.command.blobstore",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def heartbeats(*args):
    """Syntax: [storm heartbeats [cmd]]
    list PATH - lists heartbeats nodes under PATH currently in the ClusterState.
    get  PATH - Get the heartbeat data at PATH
    """
    exec_storm_class(
        "org.apache.storm.command.heartbeats",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def activate(*args):
    """Syntax: [storm activate topology-name]
    Activates the specified topology's spouts.
    """
    if not args:
        print_usage(command="activate")
        sys.exit(2)
    exec_storm_class(
        "org.apache.storm.command.activate",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def set_log_level(*args):
    """
    Dynamically change topology log levels
    Syntax: [storm set_log_level -l [logger name]=[log level][:optional timeout] -r [logger name] topology-name]
    where log level is one of:
        ALL, TRACE, DEBUG, INFO, WARN, ERROR, FATAL, OFF
    and timeout is integer seconds.
    e.g.
        ./bin/storm set_log_level -l ROOT=DEBUG:30 topology-name
        Set the root logger's level to DEBUG for 30 seconds
        ./bin/storm set_log_level -l com.myapp=WARN topology-name
        Set the com.myapp logger's level to WARN for 30 seconds
        ./bin/storm set_log_level -l com.myapp=WARN -l com.myOtherLogger=ERROR:123 topology-name
        Set the com.myapp logger's level to WARN indifinitely, and com.myOtherLogger
        to ERROR for 123 seconds
        ./bin/storm set_log_level -r com.myOtherLogger topology-name
        Clears settings, resetting back to the original level
    """
    exec_storm_class(
        "org.apache.storm.command.set_log_level",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def listtopos(*args):
    """Syntax: [storm list]
    List the running topologies and their statuses.
    """
    exec_storm_class(
        "org.apache.storm.command.list",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def deactivate(*args):
    """Syntax: [storm deactivate topology-name]
    Deactivates the specified topology's spouts.
    """
    if not args:
        print_usage(command="deactivate")
        sys.exit(2)
    exec_storm_class(
        "org.apache.storm.command.deactivate",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def rebalance(*args):
    """Syntax: [storm rebalance topology-name [-w wait-time-secs] [-n new-num-workers] [-e component=parallelism]*]
    Sometimes you may wish to spread out where the workers for a topology
    are running. For example, let's say you have a 10 node cluster running
    4 workers per node, and then let's say you add another 10 nodes to
    the cluster. You may wish to have Storm spread out the workers for the
    running topology so that each node runs 2 workers. One way to do this
    is to kill the topology and resubmit it, but Storm provides a "rebalance"
    command that provides an easier way to do this.
    Rebalance will first deactivate the topology for the duration of the
    message timeout (overridable with the -w flag) and then redistribute
    the workers evenly around the cluster. The topology will then return to
    its previous state of activation (so a deactivated topology will still
    be deactivated and an activated topology will go back to being activated).
    The rebalance command can also be used to change the parallelism of a running topology.
    Use the -n and -e switches to change the number of workers or number of executors of a component
    respectively.
    """
    if not args:
        print_usage(command="rebalance")
        sys.exit(2)
    exec_storm_class(
        "org.apache.storm.command.rebalance",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])

def get_errors(*args):
    """Syntax: [storm get-errors topology-name]
    Get the latest error from the running topology. The returned result contains
    the key value pairs for component-name and component-error for the components in error.
    The result is returned in json format.
    """
    if not args:
        print_usage(command="get_errors")
        sys.exit(2)
    exec_storm_class(
        "org.apache.storm.command.get_errors",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, os.path.join(STORM_DIR, "bin")])

def healthcheck(*args):
    """Syntax: [storm node-health-check]
    Run health checks on the local supervisor.
    """
    exec_storm_class(
        "org.apache.storm.command.HealthCheck",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, os.path.join(STORM_DIR, "bin")])

def kill_workers(*args):
    """Syntax: [storm kill_workers]
    Kill the workers running on this supervisor. This command should be run
    on a supervisor node. If the cluster is running in secure mode, then user needs
    to have admin rights on the node to be able to successfully kill all workers.
    """
    exec_storm_class(
        "org.apache.storm.command.KillWorkers",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, os.path.join(STORM_DIR, "bin")])

def shell(resourcesdir, command, *args):
    """Syntax: [storm shell resourcesdir command args]
    Archives resources to jar and uploads jar to Nimbus, and executes following arguments on "local". Useful for non JVM languages.
    eg: `storm shell resources/ python topology.py arg1 arg2`
    """
    tmpjarpath = "stormshell" + str(random.randint(0, 10000000)) + ".jar"
    os.system("jar cf %s %s" % (tmpjarpath, resourcesdir))
    runnerargs = [tmpjarpath, command]
    runnerargs.extend(args)
    exec_storm_class(
        "org.apache.storm.command.shell_submission",
        args=runnerargs,
        jvmtype="",
        extrajars=[USER_CONF_DIR],
        fork=True)
    os.system("rm " + tmpjarpath)

def repl():
    """Syntax: [storm repl]
    Opens up a Clojure REPL with the storm jars and configuration
    on the classpath. Useful for debugging.
    """
    cppaths = [CLUSTER_CONF_DIR]
    exec_storm_class("clojure.main", jvmtype="", extrajars=cppaths)

def get_log4j2_conf_dir():
    cppaths = [CLUSTER_CONF_DIR]
    storm_log4j2_conf_dir = confvalue("storm.log4j2.conf.dir", cppaths)
    if(storm_log4j2_conf_dir == None or storm_log4j2_conf_dir == "nil"):
        storm_log4j2_conf_dir = STORM_LOG4J2_CONF_DIR
    elif(not os.path.isabs(storm_log4j2_conf_dir)):
        storm_log4j2_conf_dir = os.path.join(STORM_DIR, storm_log4j2_conf_dir)
    return storm_log4j2_conf_dir

def nimbus(klass="org.apache.storm.daemon.nimbus"):
    """Syntax: [storm nimbus]
    Launches the nimbus daemon. This command should be run under
    supervision with a tool like daemontools or monit.
    See Setting up a Storm cluster for more information.
    (http://storm.apache.org/documentation/Setting-up-a-Storm-cluster)
    """
    cppaths = [CLUSTER_CONF_DIR]
    jvmopts = parse_args(confvalue("nimbus.childopts", cppaths)) + [
        "-Dlogfile.name=nimbus.log",
        "-DLog4jContextSelector=org.apache.logging.log4j.core.async.AsyncLoggerContextSelector",
        "-Dlog4j.configurationFile=" + os.path.join(get_log4j2_conf_dir(), "cluster.xml"),
        ]
    exec_storm_class(
        klass,
        jvmtype="-server",
        daemonName="nimbus",
        extrajars=cppaths,
        jvmopts=jvmopts)

def pacemaker(klass="org.apache.storm.pacemaker.pacemaker"):
    """Syntax: [storm pacemaker]
    Launches the Pacemaker daemon. This command should be run under
    supervision with a tool like daemontools or monit.
    See Setting up a Storm cluster for more information.
    (http://storm.apache.org/documentation/Setting-up-a-Storm-cluster)
    """
    cppaths = [CLUSTER_CONF_DIR]
    jvmopts = parse_args(confvalue("pacemaker.childopts", cppaths)) + [
        "-Dlogfile.name=pacemaker.log",
        "-Dlog4j.configurationFile=" + os.path.join(get_log4j2_conf_dir(), "cluster.xml"),
        ]
    exec_storm_class(
        klass,
        jvmtype="-server",
        daemonName="pacemaker",
        extrajars=cppaths,
        jvmopts=jvmopts)

def supervisor(klass="org.apache.storm.daemon.supervisor.Supervisor"):
    """Syntax: [storm supervisor]
    Launches the supervisor daemon. This command should be run
    under supervision with a tool like daemontools or monit.
    See Setting up a Storm cluster for more information.
    (http://storm.apache.org/documentation/Setting-up-a-Storm-cluster)
    """
    cppaths = [CLUSTER_CONF_DIR]
    jvmopts = parse_args(confvalue("supervisor.childopts", cppaths)) + [
        "-Dlogfile.name=" + STORM_SUPERVISOR_LOG_FILE,
        "-DLog4jContextSelector=org.apache.logging.log4j.core.async.AsyncLoggerContextSelector",
        "-Dlog4j.configurationFile=" + os.path.join(get_log4j2_conf_dir(), "cluster.xml"),
        ]
    exec_storm_class(
        klass,
        jvmtype="-server",
        daemonName="supervisor",
        extrajars=cppaths,
        jvmopts=jvmopts)

def ui():
    """Syntax: [storm ui]
    Launches the UI daemon. The UI provides a web interface for a Storm
    cluster and shows detailed stats about running topologies. This command
    should be run under supervision with a tool like daemontools or monit.
    See Setting up a Storm cluster for more information.
    (http://storm.apache.org/documentation/Setting-up-a-Storm-cluster)
    """
    cppaths = [CLUSTER_CONF_DIR]
    jvmopts = parse_args(confvalue("ui.childopts", cppaths)) + [
        "-Dlogfile.name=ui.log",
        "-DLog4jContextSelector=org.apache.logging.log4j.core.async.AsyncLoggerContextSelector",
        "-Dlog4j.configurationFile=" + os.path.join(get_log4j2_conf_dir(), "cluster.xml")
    ]
    exec_storm_class(
        "org.apache.storm.ui.core",
        jvmtype="-server",
        daemonName="ui",
        jvmopts=jvmopts,
        extrajars=[STORM_DIR, CLUSTER_CONF_DIR])

def logviewer():
    """Syntax: [storm logviewer]
    Launches the log viewer daemon. It provides a web interface for viewing
    storm log files. This command should be run under supervision with a
    tool like daemontools or monit.
    See Setting up a Storm cluster for more information.
    (http://storm.apache.org/documentation/Setting-up-a-Storm-cluster)
    """
    cppaths = [CLUSTER_CONF_DIR]
    jvmopts = parse_args(confvalue("logviewer.childopts", cppaths)) + [
        "-Dlogfile.name=logviewer.log",
        "-DLog4jContextSelector=org.apache.logging.log4j.core.async.AsyncLoggerContextSelector",
        "-Dlog4j.configurationFile=" + os.path.join(get_log4j2_conf_dir(), "cluster.xml")
    ]
    exec_storm_class(
        "org.apache.storm.daemon.logviewer",
        jvmtype="-server",
        daemonName="logviewer",
        jvmopts=jvmopts,
        extrajars=[STORM_DIR, CLUSTER_CONF_DIR])

def drpc():
    """Syntax: [storm drpc]
    Launches a DRPC daemon. This command should be run under supervision
    with a tool like daemontools or monit.
    See Distributed RPC for more information.
    (http://storm.apache.org/documentation/Distributed-RPC)
    """
    cppaths = [CLUSTER_CONF_DIR]
    jvmopts = parse_args(confvalue("drpc.childopts", cppaths)) + [
        "-Dlogfile.name=drpc.log",
        "-DLog4jContextSelector=org.apache.logging.log4j.core.async.AsyncLoggerContextSelector",
        "-Dlog4j.configurationFile=" + os.path.join(get_log4j2_conf_dir(), "cluster.xml")
    ]
    exec_storm_class(
        "org.apache.storm.daemon.drpc",
        jvmtype="-server",
        daemonName="drpc",
        jvmopts=jvmopts,
        extrajars=[CLUSTER_CONF_DIR])

def dev_zookeeper():
    """Syntax: [storm dev-zookeeper]
    Launches a fresh Zookeeper server using "dev.zookeeper.path" as its local dir and
    "storm.zookeeper.port" as its port. This is only intended for development/testing, the
    Zookeeper instance launched is not configured to be used in production.
    """
    cppaths = [CLUSTER_CONF_DIR]
    exec_storm_class(
        "org.apache.storm.command.dev_zookeeper",
        jvmtype="-server",
        extrajars=[CLUSTER_CONF_DIR])

def version():
  """Syntax: [storm version]

  Prints the version number of this Storm release.
  """
  cppaths = [CLUSTER_CONF_DIR]
  exec_storm_class(
       "org.apache.storm.utils.VersionInfo",
       jvmtype="",
       extrajars=[CLUSTER_CONF_DIR])

def print_classpath():
    """Syntax: [storm classpath]
    Prints the classpath used by the storm client when running commands.
    """
    print(get_classpath([]))

def monitor(*args):
    """Syntax: [storm monitor topology-name [-i interval-secs] [-m component-id] [-s stream-id] [-w [emitted | transferred]]]
    Monitor given topology's throughput interactively.
    One can specify poll-interval, component-id, stream-id, watch-item[emitted | transferred]
    By default,
        poll-interval is 4 seconds;
        all component-ids will be list;
        stream-id is 'default';
        watch-item is 'emitted';
    """
    exec_storm_class(
        "org.apache.storm.command.monitor",
        args=args,
        jvmtype="",
        extrajars=[USER_CONF_DIR, STORM_BIN_DIR])


def print_commands():
    """Print all client commands and link to documentation"""
    print("Commands:\n\t" +  "\n\t".join(sorted(COMMANDS.keys())))
    print("\nHelp: \n\thelp \n\thelp <command>")
    print("\nDocumentation for the storm client can be found at http://storm.apache.org/documentation/Command-line-client.html\n")
    print("Configs can be overridden using one or more -c flags, e.g. \"storm list -c nimbus.host=nimbus.mycompany.com\"\n")

def print_usage(command=None):
    """Print one help message or list of available commands"""
    if command != None:
        if command in COMMANDS:
            print(COMMANDS[command].__doc__ or
                  "No documentation provided for <%s>" % command)
        else:
            print("<%s> is not a valid command" % command)
    else:
        print_commands()

def unknown_command(*args):
    print("Unknown command: [storm %s]" % ' '.join(sys.argv[1:]))
    print_usage()
    sys.exit(254)

COMMANDS = {"jar": jar, "kill": kill, "shell": shell, "nimbus": nimbus, "ui": ui, "logviewer": logviewer,
            "drpc": drpc, "supervisor": supervisor, "localconfvalue": print_localconfvalue,
            "remoteconfvalue": print_remoteconfvalue, "repl": repl, "classpath": print_classpath,
            "activate": activate, "deactivate": deactivate, "rebalance": rebalance, "help": print_usage,
            "list": listtopos, "dev-zookeeper": dev_zookeeper, "version": version, "monitor": monitor,
            "upload-credentials": upload_credentials, "pacemaker": pacemaker, "heartbeats": heartbeats, "blobstore": blobstore,
            "get-errors": get_errors, "set_log_level": set_log_level, "kill_workers": kill_workers,
            "node-health-check": healthcheck, "sql": sql}

def parse_config(config_list):
    global CONFIG_OPTS
    if len(config_list) > 0:
        for config in config_list:
            CONFIG_OPTS.append(config)

def parse_config_opts(args):
    curr = args[:]
    curr.reverse()
    config_list = []
    args_list = []
    jars_list = []
    artifacts_list = []
    artifact_repositories_list = []
    proxy_url = None
    proxy_username = None
    proxy_password = None

    while len(curr) > 0:
        token = curr.pop()
        if token == "-c":
            config_list.append(curr.pop())
        elif token == "--config":
            global CONFFILE
            CONFFILE = curr.pop()
        elif token == "--jars":
            jars_list.extend(curr.pop().split(','))
        elif token == "--artifacts":
            artifacts_list.extend(curr.pop().split(','))
        elif token == "--artifactRepositories":
            artifact_repositories_list.extend(curr.pop().split(','))
        elif token == "--proxyUrl":
            proxy_url = curr.pop()
        elif token == "--proxyUsername":
            proxy_username = curr.pop()
        elif token == "--proxyPassword":
            proxy_password = curr.pop()
        else:
            args_list.append(token)

    return config_list, jars_list, artifacts_list, artifact_repositories_list, \
           proxy_url, proxy_username, proxy_password, args_list

def main():
    if len(sys.argv) <= 1:
        print_usage()
        sys.exit(-1)
    global CONFIG_OPTS, DEP_JARS_OPTS, DEP_ARTIFACTS_OPTS, DEP_ARTIFACTS_REPOSITORIES_OPTS, DEP_PROXY_URL, \
        DEP_PROXY_USERNAME, DEP_PROXY_PASSWORD
    config_list, jars_list, artifacts_list, artifact_repositories_list, proxy_url, proxy_username, \
    proxy_password, args = parse_config_opts(sys.argv[1:])
    parse_config(config_list)
    DEP_JARS_OPTS = jars_list
    DEP_ARTIFACTS_OPTS = artifacts_list
    DEP_ARTIFACTS_REPOSITORIES_OPTS = artifact_repositories_list
    DEP_PROXY_URL = proxy_url
    DEP_PROXY_USERNAME = proxy_username
    DEP_PROXY_PASSWORD = proxy_password
    COMMAND = args[0]
    ARGS = args[1:]
    (COMMANDS.get(COMMAND, unknown_command))(*ARGS)

if __name__ == "__main__":
    main()