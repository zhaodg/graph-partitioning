import os
import csv
import gzip
import shutil
import tempfile
import random
import platform
import itertools
import operator
import subprocess
import community
import numpy as np
import networkx as nx

from graph_partitioning.tools.infomap import infomap #TODO how would this work on windows?

BIN_DIRECTORY = os.path.join(os.path.dirname(__file__), "..", "bin")

def read_metis(DATA_FILENAME):

    G = nx.Graph()

    # add node weights from METIS file
    with open(DATA_FILENAME, "r") as metis:

        n = 0
        first_line = None
        has_edge_weights = False
        has_node_weights = False
        for i, line in enumerate(metis):
            if line[0] == '%':
                # ignore comments
                continue

            if not first_line:
                # read meta data from first line
                first_line = line.split()
                m_nodes = int(first_line[0])
                m_edges = int(first_line[1])
                if len(first_line) > 2:
                    # FMT has the following meanings:
                    #  0  the graph has no weights (in this case, you can omit FMT)
                    #  1  the graph has edge weights
                    # 10  the graph has node weights
                    # 11  the graph has both edge and node weights
                    file_format = first_line[2]
                    if int(file_format) == 0:
                        pass
                    elif int(file_format) == 1:
                        has_edge_weights = True
                    elif int(file_format) == 10:
                        has_node_weights = True
                    elif int(file_format) == 11:
                        has_edge_weights = True
                        has_node_weights = True
                    else:
                        assert False, "File format not supported"
                continue

            # METIS starts node count from 1, here we start from 0 by
            # subtracting 1 in the edge list and incrementing 'n' after
            # processing the line.
            if line.strip():
                e = line.split()
                if has_edge_weights and has_node_weights:
                    if len(e) > 2:
                        # create weighted edge list:
                        #  [(1, 2, {'weight':'2'}), (1, 3, {'weight':'8'})]
                        edges_split = list(zip(*[iter(e[1:])] * 2))
                        edge_list = [(n, int(v[0]) - 1, {'weight': int(v[1])}) for v in edges_split]

                        G.add_edges_from(edge_list)
                        G.node[n]['weight'] = int(e[0])
                    else:
                        # no edges
                        G.add_nodes_from([n], weight=int(e[0]))

                elif has_edge_weights and not has_node_weights:
                    if len(e) > 0:
                        edges_split = list(zip(*[iter(e)] * 2))
                        edge_list = [(n, int(v[0]) - 1, {'weight': int(v[1])}) for v in edges_split]

                        G.add_edges_from(edge_list)
                        G.node[n]['weight'] = 1.0
                    else:
                        G.add_nodes_from([n], weight=1.0)

                elif not has_edge_weights and has_node_weights:
                    pass
                else:
                    edge_list = [(n, int(v) - 1, {'weight':1.0}) for v in e]
                    G.add_edges_from(edge_list)
                    G.node[n]['weight'] = 1.0
            else:
                # blank line indicates no node weight
                G.add_nodes_from([n], weight=1.0)
            n += 1

    # sanity check
    assert (m_nodes == G.number_of_nodes()), "Expected {} nodes, networkx graph contains {} nodes".format(m_nodes, G.number_of_nodes())
    assert (m_edges == G.number_of_edges()), "Expected {} edges, networkx graph contains {} edges".format(m_edges, G.number_of_edges())

    return G


def bincount_assigned(graph, assignments, num_partitions):
    parts = [0] * num_partitions
    for n in graph.nodes_iter(data=True):
        node = n[0]
        if 'weight' in n[1]:
            weight = n[1]['weight']
        else:
            weight = 1
        if assignments[node] >= 0:
            parts[assignments[node]] += weight

    return parts


rpy2_loaded = False
base = None
utils = None
mgcv = None
def gam_predict(location_csv, prediction_file, num_arrived, k_value):

    if not rpy2_loaded:
        from rpy2.robjects import Formula
        from rpy2.robjects.packages import importr
        base = importr('base')
        utils = importr('utils')
        mgcv = importr('mgcv')

    location_filename = location_csv #os.path.basename(location_csv)
    prediction_filename = prediction_file #os.path.basename(prediction_file)

    # Setup
    #base.setwd(os.path.dirname(location_csv))
    loc = utils.read_csv(location_filename, header=False, nrows=num_arrived)
    pred = utils.read_csv(prediction_filename, header=False, nrows=num_arrived)
    pop = base.cbind(pred, loc)
    pop.colnames = ["shelter","x","y"]

    # GAM
    formula = Formula('shelter~s(x,y,k={})'.format(k_value))
    m = mgcv.gam(formula, family="binomial", method="REML", data=pop)

    # Predict for everyone
    loc = utils.read_csv(location_filename, header=False)
    pred = utils.read_csv(prediction_filename, header=False)
    newd = base.cbind(pred, loc)
    newd.colnames = ["shelter","x","y"]
    result = mgcv.predict_gam(m, newd, type="response", se_fit=False)

    return list(result)


def score(graph, assignment, num_partitions=None):
    """Compute the score given an assignment of vertices.

    N nodes are assigned to clusters 0 to K-1.

    assignment: Vector where N[i] is the cluster node i is assigned to.
    edges: The edges in the graph, assumed to have one in each direction

    Returns: (total wasted bin space, ratio of edges cut)
    """
    if num_partitions:
        # Note: the partition counts should be divided by the number of nodes which have been partitioned
        # rather than the size of the assignments vector
        balance = np.array(bincount_assigned(graph, assignment, num_partitions))
        if graph.number_of_nodes() > 0:
            balance = balance / (graph.number_of_nodes() * 1.0)
        #balance = np.array(bincount_assigned(graph, assignment, num_partitions)) / len(assignment)
    else:
        balance = np.bincount(assignment) / len(assignment)
    waste = (np.max(balance) - balance).sum()

    left_edge_assignment = assignment.take([x[0] for x in graph.edges()]) #edges[:,0])
    right_edge_assignment = assignment.take([x[1] for x in graph.edges()]) #edges[:,1])
    mismatch = (left_edge_assignment != right_edge_assignment).sum()
    if graph.number_of_edges() > 0:
        cut_ratio = mismatch / graph.number_of_edges()
    else:
        cut_ratio = 0.0

    return (waste, cut_ratio, mismatch)


def base_metrics(G, assignments=None):
    """
    This algorithm calculates the number of edges cut and scores the communication steps. It gets
    passed a networkx graph with a 'partition' attribute defining the partition of the node.

    Communication steps described on slide 11:
    https://www.cs.fsu.edu/~engelen/courses/HPC-adv/GraphPartitioning.pdf
    """
    steps = 0
    edges_cut = 0
    seen = []
    cut_edges = []
    for n in G.nodes_iter():
        partition_seen = []
        for e in G.edges_iter(n):
            left = e[0]
            right = e[1]
            if assignments is None:
                left_partition = G.node[left]['partition']
                right_partition = G.node[right]['partition']
            else:
                left_partition = assignments[left]
                right_partition = assignments[right]

            if left_partition == right_partition:
                # right node within same partition, skip
                continue

            if (n,right) not in seen:
                # dealing with undirected graphs
                seen.append((n,right))
                seen.append((right,n))

                if left_partition != right_partition:
                    # right node in different partition
                    edges_cut += 1
                    cut_edges.append((left, right))

            if left_partition != right_partition and right_partition not in partition_seen:
                steps += 1
                partition_seen.append(right_partition)

    return (edges_cut, steps, cut_edges)

def infomapModularityComQuality(G, assignments, num_partitions):
    p = get_partition_population(G, assignments, num_partitions)
    partition_population = [p[x][0] for x in p]
    partition_score = list(range(0, num_partitions))

    temp_dir = tempfile.mkdtemp()
    #print('infomapModularityComQuality dir', temp_dir)

    partition_metrics = [0.0, 0.0, 0.0]
    total_nodes = 0

    for p in range(0, num_partitions):
        #print('partition', p)
        nodes = [i for i,x in enumerate(assignments) if x == p]
        Gsub = G.subgraph(nodes)
        # communities = {communityID:[communityNodes],...}
        communityNodes = infomapCommunityDetection(Gsub)
        sorted_nodes = sorted(nodes)

        network_path = os.path.join(temp_dir, 'network_' + str(p) + '.txt')
        community_path = os.path.join(temp_dir, 'community_' + str(p) + '.txt')

        # write the network file
        with open(network_path, 'w+') as f:
            # write node_id other_node_id edge_weight
            for node in sorted_nodes:

                try:
                    neighs = Gsub.neighbors(node)
                except Exception as e:
                    print('Gsub neighbors error: node, nodes, sorted_nodes Gsub.nodes()', node, nodes, sorted_nodes, Gsub.nodes(), G.nodes())

                for neighbor in Gsub.neighbors(node):
                    weight = 1
                    try:
                        weight = int(G.edge[node][neighbor]['weight'])
                    except Exception as err:
                        pass
                    f.write(str(node) + " " + str(neighbor) + " " + str(weight) + "\n")

        # collect nodes for each community
        #communityNodes = {}
        #for node in sorted_nodes:
        #    nodeCommunity = communities[node]
        #    if nodeCommunity in communityNodes:
        #        communityNodes[nodeCommunity].append(node)
        #    else:
        #        communityNodes[nodeCommunity] = [node]

        # write the communities file
        with open(community_path, 'w+') as f:
            for key in sorted(communityNodes.keys()):
                s = ''
                for node in sorted(communityNodes[key]):
                    if len(s):
                        s = s + " "
                    s = s + str(node)
                f.write(s + '\n')
        # run ComQuality
        com_qual_path = os.path.join(BIN_DIRECTORY, "ComQualityMetric")
        com_qual_log = os.path.join(temp_dir, 'quality.log')
        args = ["java", "CommunityQualityUpdated", "-weighted", network_path, community_path]

        with open(com_qual_log, "w+") as logwriter:
            retval = subprocess.call(
                args, cwd=com_qual_path,
                stdout=logwriter, stderr=subprocess.STDOUT)

        n_nodes = len(nodes)
        total_nodes = total_nodes + n_nodes
        # return metrics
        with open(com_qual_log, "r") as fp:
            metrics = {}
            for line in fp:
                m = [p.strip() for p in line.split(',')]
                metrics.update(dict(map(lambda y:y.split(' = '), m)))
            partition_metrics[0] += float(metrics['Q']) * (1.0 * n_nodes)
            partition_metrics[1] += float(metrics['Qds']) * (1.0 * n_nodes)
            partition_metrics[2] += float(metrics['conductance']) * (1.0 * n_nodes)

    if total_nodes:
        partition_metrics[0] = partition_metrics[0] / (1.0 * total_nodes)
        partition_metrics[1] = partition_metrics[1] / (1.0 * total_nodes)#4.0
        partition_metrics[2] = partition_metrics[2] / (1.0 * total_nodes)#4.0
    else:
        partition_metrics[0] = 0.0
        partition_metrics[1] = 0.0
        partition_metrics[2] = 0.0
    #partition_metrics[0] = partition_metrics[0] / (1.0 * num_partitions)
    #partition_metrics[1] = partition_metrics[1] / (1.0 * num_partitions)#4.0
    #partition_metrics[2] = partition_metrics[2] / (1.0 * num_partitions)#4.0

    shutil.rmtree(temp_dir)

    return [partition_metrics[0], partition_metrics[1], partition_metrics[2]]


def louvainModularityComQuality(G, assignments, num_partitions):
    p = get_partition_population(G, assignments, num_partitions)
    partition_population = [p[x][0] for x in p]
    partition_score = list(range(0, num_partitions))

    temp_dir = tempfile.mkdtemp()
    #print('louvainModularityComQuality dir', temp_dir)

    partition_metrics = [0.0, 0.0, 0.0]

    for p in range(0, num_partitions):
        #print('partition', p)
        nodes = [i for i,x in enumerate(assignments) if x == p]
        Gsub = G.subgraph(nodes)
        communities = community.best_partition(Gsub)
        sorted_nodes = sorted(nodes)

        network_path = os.path.join(temp_dir, 'network_' + str(p) + '.txt')
        community_path = os.path.join(temp_dir, 'community_' + str(p) + '.txt')

        # write the network file
        with open(network_path, 'w+') as f:
            # write node_id other_node_id edge_weight
            for node in sorted_nodes:
                for neighbor in Gsub.neighbors(node):
                    weight = 1
                    try:
                        weight = int(G.edge[node][neighbor]['weight'])
                    except Exception as err:
                        pass
                    f.write(str(node) + " " + str(neighbor) + " " + str(weight) + "\n")

        # collect nodes for each community
        communityNodes = {}
        for node in sorted_nodes:
            nodeCommunity = communities[node]
            if nodeCommunity in communityNodes:
                communityNodes[nodeCommunity].append(node)
            else:
                communityNodes[nodeCommunity] = [node]

        # write the communities file
        with open(community_path, 'w+') as f:
            for key in sorted(communityNodes.keys()):
                s = ''
                for node in sorted(communityNodes[key]):
                    if len(s):
                        s = s + " "
                    s = s + str(node)
                f.write(s + '\n')
        # run ComQuality
        com_qual_path = os.path.join(BIN_DIRECTORY, "ComQualityMetric")
        com_qual_log = os.path.join(temp_dir, 'quality.log')
        args = ["java", "CommunityQualityUpdated", "-weighted", network_path, community_path]

        with open(com_qual_log, "w+") as logwriter:
            retval = subprocess.call(
                args, cwd=com_qual_path,
                stdout=logwriter, stderr=subprocess.STDOUT)

        # return metrics
        with open(com_qual_log, "r") as fp:
            metrics = {}
            for line in fp:
                m = [p.strip() for p in line.split(',')]
                metrics.update(dict(map(lambda y:y.split(' = '), m)))
            partition_metrics[0] += float(metrics['Q'])
            partition_metrics[1] += float(metrics['Qds'])
            partition_metrics[2] += float(metrics['conductance'])

    partition_metrics[0] = partition_metrics[0] / (1.0 * num_partitions)
    partition_metrics[1] = partition_metrics[1] / (1.0 * num_partitions)#4.0
    partition_metrics[2] = partition_metrics[2] / (1.0 * num_partitions)#4.0

    shutil.rmtree(temp_dir)

    return [partition_metrics[0], partition_metrics[1], partition_metrics[2]]

def modularityComQuality(G, assignments, num_partitions):
    p = get_partition_population(G, assignments, num_partitions)
    partition_population = [p[x][0] for x in p]
    partition_score = list(range(0, num_partitions))

    for p in range(0, num_partitions):
        nodes = [i for i,x in enumerate(assignments) if x == p]
        Gsub = G.subgraph(nodes)
        communities = community.best_partition(Gsub)
        sorted_nodes = sorted(nodes)
        print("partition nodes for p", p)
        print(sorted_nodes)
        with open('/Users/voreno/Desktop/tmp/network_' + str(p) + '.txt', 'w+') as f:
            for node in sorted_nodes:
                for neighbor in Gsub.neighbors(node):
                    weight = 1
                    try:
                        weight = int(G.edge[node][neighbor]['weight'])
                    except Exception as err:
                        pass
                    f.write(str(node) + " " + str(neighbor) + " " + str(weight) + "\n")

        communityNodes = {}

        print("partition communities for p", p)
        for node in sorted_nodes:
            nodeCommunity = communities[node]
            if nodeCommunity in communityNodes:
                communityNodes[nodeCommunity].append(node)
            else:
                communityNodes[nodeCommunity] = [node]
        with open('/Users/voreno/Desktop/tmp/community_' + str(p) + '.txt', 'w+') as f:
            for key in sorted(communityNodes.keys()):
                s = ''
                for node in sorted(communityNodes[key]):
                    if len(s):
                        s = s + " "
                    s = s + str(node)
                f.write(s + '\n')

        adjacencyMatrix = np.array(nx.to_numpy_matrix(Gsub, sorted(Gsub.nodes())))

        # create a temporary directory and create input files results
        #temp_dir = tempfile.mkdtemp()
        #print(temp_dir)
        print("partition adjacency numbers for p", p, Gsub.number_of_nodes())

        #adjMatrixFilePath = os.path.join(temp_dir, "adjacency_matrix.txt")
        with open('/Users/voreno/Desktop/tmp/adjacency_' + str(p) + '.txt', 'w+') as f:
            for row in adjacencyMatrix:
                rowStr = ''
                for adj in row:
                    if len(rowStr):
                        rowStr += " "
                    if adj > 0:
                        rowStr += str(int(1))
                    else:
                        rowStr += str(int(0))
                f.write(rowStr + '\n')

def modularity(G, assignments=None, best_partition=False):
    if best_partition:
        part = community.best_partition(G)
    elif assignments:
        part = dict(zip(G.nodes(), assignments))
    else:
        # get assignments from Graph
        part = dict([(n[0], int(n[1]['partition'])) for n in G.nodes(data=True)])
    mod = community.modularity(part, G)
    return mod

def fix_G_for_modularity(G):
    if(G.size(weight='weight') == 0):
        for node in G.nodes():
            for neighbor in G.neighbors(node):
                if(G.edge[node][neighbor]['weight'] == 0):
                    G.edge[node][neighbor]['weight'] = 1.0

def modularity_wavg(G, assignments, num_partitions):
    """
    Return a weighted average across all partitions for modularity score
    """
    p = get_partition_population(G, assignments, num_partitions)
    partition_population = [p[x][0] for x in p]
    partition_score = list(range(0, num_partitions))

    for p in range(0, num_partitions):
        nodes = [i for i,x in enumerate(assignments) if x == p]
        #Gsub = G.subgraph(nodes)
        #partition_score[p] = modularity(Gsub, best_partition=True)
        # modularity crashes in the community package if Gsub has no nodes
        Gsub = G.subgraph(nodes)
        '''
        Debug code for crashes due to total graph size = 0.0 (edges have a 'weight' data parameters, but all values == 0.0)
        This cuases community used in modularity() to crash.

        print("modularity_wavg total weights ", G.size(weight='weight'), Gsub.size(weight='weight'), Gsub.number_of_nodes(), Gsub.number_of_edges())
        if(Gsub.size(weight='weight') == 0):
            for node in Gsub.nodes():
                print('n', Gsub.node[node]['weight'])
                for neigh in Gsub.neighbors(node):
                    print('e', Gsub.edge[node][neigh]['weight'])
        '''
        if Gsub.size(weight = 'weight') == 0.0:
            # try correcting
            fix_G_for_modularity(Gsub)

        if Gsub.size(weight = 'weight') > 0.0:
            partition_score[p] = modularity(Gsub, best_partition=True)
        else:
            partition_score[p] = 1.0

    average = 0.0
    try:
        average = np.average(partition_score, weights=partition_population)
    except Exception as err:
        # paritition scores and populations of 0.0 cause exception in np.average
        pass

    return np.average(partition_score, weights=partition_population)
    return average

def modularityDensityBotta(G, num_iterations):
    # compute sparse adjacency matrix
    adjacencyMatrix = np.array(nx.to_numpy_matrix(G, sorted(G.nodes())))

    # create a temporary directory and create input files results
    temp_dir = tempfile.mkdtemp()
    print(temp_dir)

    adjMatrixFilePath = os.path.join(temp_dir, "adjacency_matrix.txt")
    with open(adjMatrixFilePath, 'w+') as f:
        for row in adjacencyMatrix:
            rowStr = ''
            for adj in row:
                if len(rowStr):
                    rowStr += " "
                rowStr += str(int(adj))
            f.write(rowStr + '\n')

    binary = os.path.join(BIN_DIRECTORY, 'bottaModularityDensity')
    if 'Linux' in platform.system():
        binary = os.path.join(binary, 'BottaModularityDensityLinux')
    elif 'Darwin' in platform.system():
        binary = os.path.join(binary, 'BottaModularityDensityMacOS')

    modularityArgs = [binary, adjMatrixFilePath, str(G.number_of_nodes()), str(num_iterations)]

    print(modularityArgs)

    retval = subprocess.call(modularityArgs, cwd=temp_dir)
    print(retval)
    # clean_up
    #shutil.rmtree(temp_dir)


def loneliness_score(G, loneliness_score_param):
    total = 0
    count = 0
    for n in G.nodes():
        node_edges = len(G[n])
        score = 1 - ((1 / (node_edges + 1)**loneliness_score_param))
        total += score
        count += 1

    # average for partition
    if count == 0:
        return 0.0
    return total / count

def loneliness_score_wavg(G, loneliness_score_param, assignments, num_partitions):
    """
    Return a weighted average across all partitions for loneliness score
    """

    p = get_partition_population(G, assignments, num_partitions)
    partition_population = [p[x][0] for x in p]
    partition_score = list(range(0, num_partitions))

    for p in range(0, num_partitions):
        nodes = [i for i,x in enumerate(assignments) if x == p]
        Gsub = G.subgraph(nodes)
        partition_score[p] = loneliness_score(Gsub, loneliness_score_param)

    average = 0.0
    try:
        average = np.average(partition_score, weights=partition_population)
    except Exception as err:
        # assignments has no partitions
        pass
    #return np.average(partition_score, weights=partition_population)
    return average

def wavg_max_perm(G, assignments, num_partitions):
    partition_nodes = {}
    for node in G.nodes():
        assignment = assignments[node]
        if assignment >= 0 and assignment < num_partitions:
            if assignment in partition_nodes:
                partition_nodes[assignment].append(node)
            else:
                partition_nodes[assignment] = [node]
    score = 0.0
    total = 0
    for partition in partition_nodes.keys():
        gsub = G.subgraph(partition_nodes[partition])
        n_nodes = len(partition_nodes[partition])

        partition_max_perm = float(run_max_perm(gsub))
        #print('pmp', partition_max_perm)
        partition_max_perm = n_nodes * partition_max_perm
        score = score + partition_max_perm

        #score += (n_nodes * float())
        total += n_nodes

    if total > 0:
        return score / total
    return 0.0

def run_max_perm(G, relabel_nodes=False):
    max_perm = 0.0
    temp_dir = tempfile.mkdtemp()
    edges_filename = os.path.join(temp_dir, "edges-maxperm.txt")

    # MaxPerm requires nodes in sequential order
    relabel_nodes = True # Do the relabel as maxperm wants nodes labelled 0...n-1 where n = G.number_of_nodes()
    Gcopy = None
    if relabel_nodes:
        #mapping = dict(zip(G.nodes(), range(0, len(G.nodes()))))
        #print("mapping", mapping)

        nodes = sorted(G.nodes())
        nodeMapping = {}
        for i, n in enumerate(nodes):
            #nx.relabel_nodes(G, {n:i}, copy=False)
            nodeMapping[n] = i
        #Gcopy = nx.relabel_nodes(G, nodeMapping, copy=True)
        Gcopy = nx.Graph()
        for node in G.nodes():
            Gcopy.add_node(nodeMapping[node])
            for edge in G.neighbors(node):
                Gcopy.add_edge(nodeMapping[node], nodeMapping[edge])

    # write edge list in a format for MaxPerm, tab delimited
    with open(edges_filename, "w") as outf:
        outf.write("{}\t{}\n".format(Gcopy.number_of_nodes(), Gcopy.number_of_edges()))
        for e in sorted(Gcopy.edges_iter()):
            outf.write("{}\t{}\n".format(*e))

    # cat edge list into MaxPerm bin
    with open(edges_filename, "r") as edge_file:
        args = [os.path.join(BIN_DIRECTORY, "MaxPerm", "MaxPerm")]
        with open(os.devnull, "w") as devnull:
            retval = subprocess.call(
                args, cwd=temp_dir, stdin=edge_file,
                stdout=devnull)

    # parse the output file to get the permanence metric
    with open(os.path.join(temp_dir, "output.txt"), "r") as fp:
        for i, line in enumerate(fp):
            if "Network Permanence" in line:
                max_perm = line.split()[3]
                break
    shutil.rmtree(temp_dir)
    return max_perm


def run_community_metrics(output_path, data_filename, edges_oslom_filename):
    """
    Community Quality metrics
    Use OSLOM to find clusters in edgelist, then run ComQualityMetric to get metrics.

    http://www.oslom.org/
    http://www.oslom.org/code/ReadMe.pdf for documentation on program options
    https://github.com/chenmingming/ComQualityMetric
    """
    temp_dir = ''
    oslom_bin = ''
    oslom_log = ''
    oslom_modules = ''

    if 'Linux' in platform.system():
        temp_dir = tempfile.mkdtemp()
        oslom_bin = os.path.join(BIN_DIRECTORY, "OSLOM2", "oslom_dir")
        oslom_log = os.path.join(output_path, 'oslom', data_filename + "-oslom.log")
        oslom_modules = os.path.join(output_path, 'oslom', data_filename + "-oslom-tp.txt")

    elif 'Darwin' in platform.system():
        oslom_bin = os.path.join('bin', 'OSLOM2', 'oslom_dir')
        oslom_log = os.path.join('output', 'oslom', data_filename + "-oslom.log")
        oslom_modules = os.path.join('output', 'oslom', data_filename + "-oslom-tp.txt")
        edges_oslom_filename = os.path.join('output', 'oslom', data_filename + "-edges-oslom.txt")

    args = [oslom_bin, "-f", edges_oslom_filename, "-w", "-r", "10", "-hr", "50", "-infomap", "5"]

    if 'Linux' in platform.system():
        with open(oslom_log, "w") as logwriter:
            retval = subprocess.call(
                args, cwd=os.path.join(output_path, 'oslom'),
                stdout=logwriter, stderr=subprocess.STDOUT)
        shutil.copy(os.path.join(temp_dir, "tp"), oslom_modules)
        shutil.rmtree(temp_dir)
    elif 'Darwin' in platform.system():
        with open(oslom_log, "w+") as logwriter:
            retval = subprocess.call(
                args,
                stdout=logwriter, stderr=subprocess.STDOUT)
        shutil.copy("tp", oslom_modules)
        os.remove("tp")
        os.remove("time_seed.dat")

    com_qual_path = os.path.join(BIN_DIRECTORY, "ComQualityMetric")
    com_qual_log = os.path.join(output_path, 'oslom', data_filename + "-CommunityQuality.log")

    args = []
    if 'Linux' in platform.system():
        args = ["java", "OverlappingCommunityQuality", "-weighted", edges_oslom_filename, oslom_modules]
    elif 'Darwin'  in platform.system():
        args = ["java", "OverlappingCommunityQuality", "-weighted", os.path.join('..', '..', edges_oslom_filename), os.path.join('..', '..',oslom_modules)]

    with open(com_qual_log, "w+") as logwriter:
        retval = subprocess.call(
            args, cwd=com_qual_path,
            stdout=logwriter, stderr=subprocess.STDOUT)

    if retval != 0:
        with open(com_qual_log, "r") as log:
            e = log.read().replace('\n', '')
        raise Exception(e)

    with open(com_qual_log, "r") as fp:
        metrics = {}
        for line in fp:
            if ' = ' in line:
                m = [p.strip() for p in line.split(',')]
                metrics.update(dict(map(lambda y:y.split(' = '), m)))

    return metrics

def get_partition_population(graph, assignments, num_partitions):
    population = {}
    if -1 not in assignments:
        nodes = np.bincount(assignments, minlength=num_partitions).astype(np.float32)
        weights = bincount_assigned(graph, assignments, num_partitions)

    else:
        nodes = [0] * num_partitions
        weights = [0] * num_partitions
        for i in range(0, len(assignments)):
            if assignments[i] >= 0:
                nodes[assignments[i]] += 1
                # get the node's weight
                try:
                    weights[assignments[i]] += graph.node[i]['weight']
                except Exception as err:
                    weights[assignments[i]] += 1.0

    for p in range(0, num_partitions):
        population[p] = (nodes[p], weights[p])

    return population

def print_partitions(graph, assignments, num_partitions):
    population = get_partition_population(graph, assignments, num_partitions)

    print("\nPartitions - nodes (weight):")
    for p in population:
        print("P{}: {} ({})".format(p, population[p][0], population[p][1]))

def fixed_width_print(arr):
    print("[", end='')
    for x in range(0, len(arr)):
        if arr[x] >= 0:
            print(" ", end='')

        print("{}".format(arr[x]), end='')

        if x != len(arr)-1:
            print(" ", end='')
    print("]")

def write_assignment_file(outfolder, filepath, assignments):
    if not os.path.exists(outfolder):
        os.makedirs(outfolder)
    with open(filepath, 'w+') as outF:
        for i, assignment in enumerate(assignments):
            outF.write(str(i) + " " + str(assignment) + "\n")

def write_partition_file(outfolder, filepath, assignments, partition):
    if not os.path.exists(outfolder):
        os.makedirs(outfolder)
    with open(filepath, 'w+') as outF:
        for i, assignment in enumerate(assignments):
            if(assignment == partition):
                outF.write(str(i) + " " + str(assignment) + "\n")

def write_graph_files(output_path, data_filename, G, quiet=False, relabel_nodes=False):

    if not os.path.exists(output_path):
        os.makedirs(output_path)
    if not os.path.exists(os.path.join(output_path, 'oslom')):
        os.makedirs(os.path.join(output_path, 'oslom'))
    if not os.path.exists(os.path.join(output_path, 'graphs')):
        os.makedirs(os.path.join(output_path, 'graphs'))

    # write to GML file
    gml_filename = os.path.join(output_path, 'graphs', data_filename + "-graph.gml")
    nx.write_gml(G, gml_filename)

    # write assignments into a file with a single column
    assignments_filename = os.path.join(output_path, 'graphs', data_filename + "-assignments.txt")
    with open(assignments_filename, "w") as outf:
        for n in G.nodes_iter(data=True):
            outf.write("{}\n".format(n[1]["partition"]))

    # write edge list in a format for OSLOM, tab delimited
    edges_oslom_filename = os.path.join(output_path, 'oslom', data_filename + "-edges-oslom.txt")
    with open(edges_oslom_filename, "w") as outf:
        for e in G.edges_iter(data=True):
            outf.write("{}\t{}\t{}\n".format(e[0], e[1], e[2]["weight"]))

    if not quiet:
        print("Writing GML file: {}".format(gml_filename))
        print("Writing assignments: {}".format(assignments_filename))
        print("Writing edge list (for OSLOM): {}".format(edges_oslom_filename))

    return edges_oslom_filename

def write_metrics_csv(filename, fields, metrics):
    if not os.path.exists(filename):
        with open(filename, "w", newline='') as outf:
            csv_writer = csv.DictWriter(outf, fieldnames=fields)
            csv_writer.writeheader()
    with open(filename, "a", newline='') as outf:
        csv_writer = csv.DictWriter(outf, fieldnames=fields)
        csv_writer.writerow(metrics)

import matplotlib.cm as cmx
import matplotlib.colors as colors
def get_cmap(N):
    '''Returns a function that maps each index in 0, 1, ... N-1 to a distinct
    RGB color.
    source: http://stackoverflow.com/a/25628397 '''
    color_norm  = colors.Normalize(vmin=0, vmax=N-1)
    scalar_map = cmx.ScalarMappable(norm=color_norm, cmap='hsv')
    def map_index_to_rgb_color(index):
        return scalar_map.to_rgba(index)
    return map_index_to_rgb_color

def generateRandIntArray(minValue, maxValueExcluded, size):
    # generates an np.array of size=size of random integers in the range [minValue, maxValueExcluded)
    # i.e. min = 0, max = 10 generates random int values 0-9 included
    return np.random.randint(minValue, maxValueExcluded, size)


# fscores
from sklearn.metrics import f1_score
from scipy.optimize import linear_sum_assignment

def fscores2(predictionModel, assignments, num_partitions):
    prediction = np.array(predictionModel, copy=True)
    batch = np.array(assignments, copy=True)

    pm = []
    btch = []
    for i, partition in enumerate(batch):
        if partition >= 0:
            pm.append(prediction[i])
            btch.append(partition)

    prediction = np.array(pm)
    batch = np.array(btch)

    #print('computing fscores', len(prediction), len(batch))

    fscore = f1_score(prediction, batch, average='weighted')
    #fscore = 0.0

    fscorematrix = []
    for i in range(0, num_partitions):
        fi = []
        fi_correct = []
        for j in range(0, num_partitions):
            batch_ij = relabelArray(batch, i, j)
            #fi.append(1.0 - 0.0)
            #print("fmatrix", len(prediction), len(batch_ij))
            fi.append(1.0 - f1_score(prediction, batch_ij, average='weighted'))
        fscorematrix.append(fi)

    cost = np.array(fscorematrix)
    row_ind, col_ind = linear_sum_assignment(cost)

    relabelled_batch = batch

    relabel_done = {}
    for i, row in enumerate(row_ind):
        # check if done already
        col = col_ind[i]
        if col == row:
            continue

        if(col < row):
            if col in relabel_done:
                #if row in relabel_done[col]:
                continue
            relabel_done[col] = row
        else:
            if row in relabel_done:
                continue
            relabel_done[row] = col
        relabelled_batch = relabelArray(relabelled_batch, row, col_ind[i])

    #print("computing fscores relabelled", len(prediction), len(relabelled_batch))
    fscore_relabelled = f1_score(prediction, relabelled_batch, average='weighted')
    #fscore_relabelled = 0.0
    return(fscore, fscore_relabelled)

def fscores(predictionModel, assignments, num_partitions):
    prediction = np.array(predictionModel, copy=True)
    batch = np.array(assignments, copy=True)

    predModelPartition = []
    batchPartition = []

    # extract only the partitions for the nodes that have arrived
    for i, partition in enumerate(batch):
        if partition >= 0:
            predModelPartition.append(prediction[i])
            batchPartition.append(partition)

    predModelPartition = np.array(predModelPartition)
    batchPartition = np.array(batchPartition)

    # compute fscore
    fscore = f1_score(predModelPartition, batchPartition, average='weighted')

    # compute relabelled fscore
    fscore_relabelled = fscore_relabel(predModelPartition, batchPartition, num_partitions)

    return (fscore, fscore_relabelled)

def relabelArray(array, v1, v2):
    newArr = []
    for val in array:
        if(val == v1):
            newArr.append(v2)
        elif (val == v2):
            newArr.append(v1)
        else:
            newArr.append(val)
    return newArr

def fscore_relabel(predictionModel, batch, num_partitions):
    fscorematrix = []
    for i in range(0, num_partitions):
        fi = []
        for j in range(0, num_partitions):
            batch_ij = relabelArray(batch, i, j)
            fi.append(1.0 - f1_score(predictionModel, batch_ij, average='weighted'))
        fscorematrix.append(fi)

    fscorematrix = np.array(fscorematrix)
    # hungarian algorithm
    row_ind, col_ind = linear_sum_assignment(fscorematrix)
    #print('Hungarian rows', row_ind)
    #print('Hungarian cols', col_ind)

    relabelled_batch = batch

    relabel_done = {} # stores which combination of partition values have been swapped
    for i, row in enumerate(row_ind):
        # check if done already
        col = col_ind[i]
        if col == row:
            continue
        if(col < row):
            if col in relabel_done:
                #if row in relabel_done[col]:
                continue
            relabel_done[col] = row
        else:
            if row in relabel_done:
                continue
            relabel_done[row] = col

        relabelled_batch = relabel(relabelled_batch, row, col_ind[i])

    return f1_score(predictionModel, relabelled_batch, average='weighted')


def leverage_centrality(graph):
    '''
    The leverage centrality in this paper is defined as a measure of the relationship between
    the degree of a given node (ki) and the degree of each of its neighbors (kj),
    averaged over all neighbors (Ni).

    The algorithm runs as following:
    1. compute degrees of each node (number of edges for each node) (ki)
    2. for each node, compute the mean([ki - kj]/[ki + kj]) for each neighbour j of node i, which is the leverage score
    '''

    n = graph.number_of_nodes()
    k = graph.degree() # returns dict of {node:degree} values
    leverage = {}
    for i,n in enumerate(sorted(graph.nodes())):
        # iterate over each node of graph, sorted
        ki = k[n] # get the degree of node n
        ni = 0 # neighbor count
        li = 0.0 # leverage for this node
        if ki > 0:
            # ki > 0 since it has some neighbors
            # kj can never be 0 since it is connected to ki
            # ki + kj >= 2
            # the fraction ki - kj
            for neighbor in graph.neighbors(n):
                ni += 1
                kj = k[neighbor]
                li = li + ((ki - kj) / (ki + kj))
            li = li / ki
        else:
            li = -1000.0
        leverage[n] = li
    return sorted(leverage.items(), key=operator.itemgetter(1),reverse=True)

def reorder_nodes_based_on_leverage_centrality(graph_leverage_scores, batch):
    reordered_nodes = []
    outliers = []
    for leverage in graph_leverage_scores:
        node = leverage[0]
        li = leverage[1]
        if node in batch:
            if li == -1000.0:
                outliers.append(node)
            else:
                reordered_nodes.append(leverage[0])
    if len(outliers) > 0:
        reordered_nodes = reordered_nodes + random.sample(outliers, len(outliers))
    return reordered_nodes


def infomapCommunityDetection(graph):
    """
    Partition network with the Infomap algorithm.
    Returns a dictionary of node lists. The key is the community ID and the list contains all the sorted nodes belonging to that community
    """

    #print('Calling infomap detection for nodes', graph.nodes())

    infomapWrapper = infomap.Infomap("--two-level --silent")
    graph_nodes = graph.nodes()

    # build infomap network from networkX graph
    for e in graph.edges_iter():
        infomapWrapper.addLink(*e)

    # find the communities with Infomap
    infomapWrapper.run();
    tree = infomapWrapper.tree

    #print("Found %d modules with codelength: %f" % (tree.numTopModules(), tree.codelength()))
    communities = {}
    for node in tree.leafIter():
        node_id = node.originalLeafIndex
        if node_id in graph_nodes:
            community = node.moduleIndex()
            if community in communities:
                communities[community].append(node_id)
            else:
                communities[community] = [node_id]
    return communities;

def ratherBeSomewhereElseMetric(RBSE_list):
    total = 0
    rbse = 0
    for i, val in enumerate(RBSE_list):
        if val < 0:
            continue
        if val == 1:
            rbse += 1
        total += 1

    if total == 0:
        return 0.0
    else:
        return (float(rbse) / float(total))

def ratherBeSomewhereElseList(graph, assignments, num_partitions):
    RBSE = np.zeros(len(assignments), dtype=int).tolist()

    for nodeID, assignment in enumerate(assignments):
        if assignment < 0:
            RBSE[nodeID] = -1
            continue

        neighbors = graph.neighbors(nodeID)

        # initialise partition scores
        partition_scores = {}
        for i in range(0, num_partitions):
            partition_scores[i] = 0

        # analyse the partition of each neighbor
        for neighbor in neighbors:
            neighbor_assignment = assignments[neighbor]

            edge_weight = 1.0
            try:
                edge_weight = graph.get_edge_data(nodeID, neighbor)['weight']
            except Exception as err:
                pass

            if neighbor_assignment >= 0:
                partition_scores[neighbor_assignment] += edge_weight

        current_partition_score = partition_scores[assignment]

        for partition in partition_scores.keys():
            score = partition_scores[partition]

            if partition != assignment:
                if score > current_partition_score:
                    RBSE[nodeID] = 1
                    break
    return RBSE


def savePredictionFile(outFilePath, assignments):
    with open(outFilePath, 'w+') as f:
        for partition in assignments:
            f.write(str(partition) + '\n')
