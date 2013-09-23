import sys
import re
import logging
import multiprocessing

import pandas as p
import numpy as np

from itertools import tee, izip, product, chain

from Bio import SeqIO

from sklearn.mixture import GMM
from sklearn.decomposition import PCA

from concoct.output import Output

def parallelized_cluster(args):
    c, cv_type,inits,iters,transform_filter= args
    #Run GMM on the pca transform of contigs with kmer count greater
    #than threshold
    gmm = GMM(n_components=c, covariance_type=cv_type, n_init=inits,
              n_iter=iters).fit(transform_filter)
    bic = gmm.bic(transform_filter)
    if gmm.converged_:
        logging.info("Cluster {0} converged".format(c))
    else:
        logging.warning("Cluster {0} did not converge".format(c))
        print >> sys.stderr, "Cluster {0} did not converge".format(c)
    return bic,c, gmm.converged_


def cluster(comp_file, cov_file, kmer_len, threshold, 
            read_length, clusters_range, cov_range, 
            split_pca, inits, iters, outdir, pipe,
            max_n_processors, pca_components, args=None):
    #Run this code if we are 
    # 1. using MPI and are rank 0 
    # or 
    # 2. if we are not using MPI
    if (max_n_processors.use_mpi and max_n_processors.rank==0) or not max_n_processors.use_mpi:
        Output(outdir,args)
        #Composition
        #Generate kmer dictionary
        feature_mapping, nr_features = generate_feature_mapping(kmer_len)
        #Count lines in composition file
        count_re = re.compile("^>")
        seq_count = 0
        with open(comp_file) as fh:
            for line in fh:
                if re.match(count_re,line):
                    seq_count += 1
    
        #Initialize with ones since we do pseudo count, we have i contigs as rows
        #and j features as columns
        composition = np.ones((seq_count,nr_features))
        
        
        contigs_id = []
        for i,seq in enumerate(SeqIO.parse(comp_file,"fasta")):
            contigs_id.append(seq.id)
            for kmer_tuple in window(seq.seq.tostring().upper(),kmer_len):
                composition[i,feature_mapping["".join(kmer_tuple)]] += 1
        composition = p.DataFrame(composition,index=contigs_id,dtype=float)
    
        # save contig lengths, used for pseudo counts in coverage
        contig_lengths = composition.sum(axis=1)
    
        #Select contigs to cluster on
        threshold_filter = composition.sum(axis=1) > threshold
        
        #log(p_ij) = log[(X_ij +1) / rowSum(X_ij+1)]
        composition = np.log(composition.divide(composition.sum(axis=1),axis=0))
        
        logging.info('Successfully loaded composition data.')
        #Coverage import, file has header and contig ids as index
        #Assume datafile is in coverage format without pseudo counts
        cov = p.read_table(cov_file,header=0,index_col=0)
        if cov_range is None:
            cov_range = (cov.columns[0],cov.columns[-1])
    
        # Log transform and add pseudo counts corresponding to one 100bp read
        cov.ix[:,cov_range[0]:cov_range[1]] = np.log(
            cov.ix[:,cov_range[0]:cov_range[1]].add(
                (100/contig_lengths),
                axis='index'))
    
        logging.info('Successfully loaded coverage data.')
    
    
        joined = composition.join(
            cov.ix[:,cov_range[0]:cov_range[1]],how="inner")
        if split_pca:
            cov_pca = PCA(n_components=pca_components[0]).fit(
                cov[threshold_filter].ix[:,cov_range[0]:cov_range[1]])
            transform_filter_cov = cov_pca.transform(
                cov[threshold_filter].ix[:,cov_range[0]:cov_range[1]])

            transform_filter_cov = p.DataFrame(transform_filter_cov,
                                               index=cov[threshold_filter].index)
            transform_filter_cov = transform_filter_cov.rename(
                columns=lambda x: 'cov_'+str(x))

            comp_pca = PCA(n_components=pca_components[1]).fit(
                composition[threshold_filter])
            transform_filter_comp = comp_pca.transform(composition[threshold_filter])
            transform_filter_comp = p.DataFrame(transform_filter_comp,
                                                index=composition[threshold_filter].index)
            transform_filter_comp = transform_filter_comp.rename(
                columns=lambda x: 'comp_'+str(x))
            transform_filter = transform_filter_comp.join(
                transform_filter_cov, how='inner')
        else:
            #PCA on the contigs that have kmer count greater than threshold
            pca = PCA(n_components=pca_components).fit(joined[threshold_filter])
            transform_filter = pca.transform(joined[threshold_filter])

        Output.write_original_data(joined[threshold_filter],threshold)
        Output.write_pca(transform_filter,
                         threshold,cov[threshold_filter].index)
        logging.info('PCA transformed data.')
    
        cv_type='full'
        cluster_args = []
        for c in clusters_range:
            cluster_args.append((c,cv_type,inits,iters,transform_filter))
    
    #This code should be executed by all threads
    if max_n_processors.use_mpi:
        if max_n_processors.rank != 0:
            cluster_args = []
        cluster_args = max_n_processors.comm.bcast(cluster_args, root=0)
        result = map(parallelized_cluster,cluster_args[max_n_processors.rank::max_n_processors.size])
        #Gather all results to root process again
        results = max_n_processors.comm.gather(result, root=0)
        if max_n_processors.rank == 0:
            results = list(chain(*results))
    
    else:
        pool = multiprocessing.Pool(processes=max_n_processors.size)
        results = pool.map(parallelized_cluster,cluster_args)
    #Run this code if we are 
    # 1. using MPI and are rank 0 
    # or 
    # 2. if we are not using MPI
    if (max_n_processors.use_mpi and max_n_processors.rank==0) or not max_n_processors.use_mpi:
        bics = [(r[0],r[1]) for r in results]
        Output.write_bic(bics)
        min_bic,optimal_c = min(bics,key=lambda x: x[0])
        gmm = GMM(n_components=optimal_c,covariance_type=cv_type,n_init=inits,
                  n_iter=iters).fit(transform_filter)
    
        if split_pca:
            # Transform both unfiltered datasets separately before joining
            transform_comp = comp_pca.transform(composition)
            transform_cov = cov_pca.transform(cov)
            transform_comp = p.DataFrame(transform_comp,
                                         index=composition.index)
            transform_cov = p.DataFrame(transform_cov,
                                        index=cov.index)
            # Renaming is necessary so no columns have the same name
            transform_comp = transform_comp.rename(
                columns = lambda x: 'comp_'+str(x))
            transform_cov = transform_cov.rename(
                columns = lambda x: 'cov_'+str(x))
            joined_transform = transform_comp.join(
                transform_cov, how='inner')

            joined["clustering"] = gmm.predict(joined_transform)
                        
        else:
            joined["clustering"] = gmm.predict(pca.transform(joined))
            Output.write_cluster_means(pca.inverse_transform(gmm.means_),
                                       threshold,c)
        # Covariance matrix is three dimensional if full
        if cv_type == 'full':
            for i,v in enumerate(gmm.covars_):
                if not split_pca:
                    Output.write_cluster_variance(pca.inverse_transform(v),
                                                  threshold,i)
                Output.write_cluster_pca_variances(v,threshold,i)
        else:
            # Not implemented yet
            pass
            
        Output.write_clustering(joined,threshold_filter,threshold,c,pipe)
        Output.write_cluster_pca_means(gmm.means_,threshold,c)
            
        pp = gmm.predict_proba(transform_filter)
    
        Output.write_cluster_responsibilities(
            pp,
            threshold,c)
        logging.info("CONCOCT Finished")


def generate_feature_mapping(kmer_len):
    BASE_COMPLEMENT = {"A":"T","T":"A","G":"C","C":"G"}
    kmer_hash = {}
    counter = 0
    for kmer in product("ATGC",repeat=kmer_len):
        kmer = ''.join(kmer)
        if kmer not in kmer_hash:
            kmer_hash[kmer] = counter
            rev_compl = ''.join([BASE_COMPLEMENT[x] for x in reversed(kmer)])
            kmer_hash[rev_compl] = counter
            counter += 1
    return kmer_hash, counter+1

def window(seq,n):
    els = tee(seq,n)
    for i,el in enumerate(els):
        for _ in xrange(i):
            next(el, None)
    return izip(*els)
