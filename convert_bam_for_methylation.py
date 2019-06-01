#! /usr/bin/env python3 
import os
import sys
import math
import bisect
import argparse
import gzip
import numpy as np
from collections import namedtuple
from methylbed_utils import MethRead,make_coord,bed_to_coord,coord_to_bed
import pysam
from Bio import SeqIO
import re
import multiprocessing as mp
import time
start_time = time.time()

def parseArgs() :
    # dir of source code
    srcpath=sys.argv[0]
    srcdir=os.path.dirname(os.path.abspath(srcpath))
    # parser
    parser = argparse.ArgumentParser(
            description='convert bam sequences to allow visualization of methylation on igv via bisulfite mode')
    parser.add_argument('-t','--threads',type=int,required=False,default=2, 
            help="number of parallel processes (default : 2 )")
    parser.add_argument('-v','--verbose', action='store_true',default=False,
            help="verbose output")
    parser.add_argument('-c','--cpg',type=os.path.abspath,required=True,
            help="gpc methylation bed - sorted, bgzipped, and indexed")
    parser.add_argument('-g','--gpc',type=os.path.abspath,required=False,
            default=None,help="gpc methylation bed - sorted, bgzipped, and indexed")
    parser.add_argument('-b','--bam',type=os.path.abspath,required=True,
            help="bam file - sorted and indexed")
    parser.add_argument('-f','--fasta',type=os.path.abspath,required=False,
            help="fasta file, for minimap2 alignments without --MD option")
    parser.add_argument('-w','--window',type=str,required=False, 
            help="window from index file to extract [chrom:start-end]")
    parser.add_argument('-r','--regions',type=argparse.FileType('r'),required=False, 
            help="windows in bed file (default: stdin)")
    parser.add_argument('-o','--out',type=str,required=False,default="stdout",
            help="output bam file (default: stdout)")
    parser.add_argument('--all',action='store_true',default=False,
            help="print reads with no methylation data (default False)")
    # parse args
    args = parser.parse_args()
    args.srcdir=srcdir
    return args

# https://stackoverflow.com/questions/107705/disable-output-buffering
class Unbuffered(object):
   def __init__(self, stream):
       self.stream = stream
   def write(self, data):
       self.stream.write(data)
       self.stream.flush()
   def writelines(self, datas):
       self.stream.writelines(datas)
       self.stream.flush()
   def __getattr__(self, attr):
       return getattr(self.stream, attr)


# https://stackoverflow.com/questions/13446445/python-multiprocessing-safely-writing-to-a-file
def listener(q,inbam,outbam,verbose=False) :
    '''listens for messages on the q, writes to file. '''
    if verbose : print("writing output to {}".format(outbam),file=sys.stderr)
    if outbam == "stdout" :
        # write output to stdout
        f = sys.stdout
        with pysam.AlignmentFile(inbam,'rb') as fh :
            print(fh.header,file=f)
        def printread(m,out_fh) :
            print(m,file=out_fh)
    else : 
        with pysam.AlignmentFile(inbam,'rb') as fh :
            f = pysam.AlignmentFile(outbam, 'wb',template = fh) 
        def printread(m,out_fh) :
            read = pysam.AlignedSegment.fromstring(m,out_fh.header)
            out_fh.write(read)
    qname_list= list()
    def index(a,x) :
        i = bisect.bisect_left(a,x)
        if i != len(a) and a[i] == x :
            return True
        return False
    while True:
        m = q.get()
        if m == 'kill':
            break
        fields = m.split("\t")
        qname = ':'.join([fields[0],fields[2],fields[3]])
        if not index(qname_list,qname) : 
#            if verbose: print(qname,file=sys.stderr)
            bisect.insort(qname_list,qname)
            printread(m,f)
#        else : 
#            if verbose: print("{} already written".format(qname),file=sys.stderr)
        q.task_done()
    if verbose : print("total {} reads processed".format(len(qname_list)),file=sys.stderr)
    f.close()
    q.task_done()

def get_windows_from_bam(bampath,winsize = 100000) :
    with pysam.AlignmentFile(bampath,"rb") as bam :
        stats = bam.get_index_statistics()
        lengths = bam.lengths
        coords = list()
        for stat,contigsize in zip(stats,lengths) :
            # only fetch from contigs that have mapped reads
            if stat.mapped == 0 :
                continue
            numbins = math.ceil(contigsize/winsize)
            wins = [ [x*winsize+1,(x+1)*winsize] for x in range(numbins) ]
            wins[-1][1] = contigsize
            for win in wins :
                coords.append(make_coord(stat.contig,win[0],win[1]))
    return coords

def read_tabix(fpath,window) :
    with pysam.TabixFile(fpath) as tabix :
        entries = [x for x in tabix.fetch(window)]
    reads = [MethRead(x) for x in entries]
    rdict = dict()
    # for split-reads, multiple entries are recorded per read name
    for meth in reads :
        qname = meth.qname
        if qname in rdict.keys() :
            rdict[qname] = np.append(rdict[qname],meth.callarray,0)
        else : 
            rdict[qname] = meth.callarray
    return rdict

def convert_cpg(bam,cpg,gpc) :
    # only cpg
    return change_sequence(bam,cpg,"cpg")

def convert_nome(bam,cpg,gpc) :
    # cpg and gpc
    bam_cpg = change_sequence(bam,cpg,"cpg") 
    return change_sequence(bam_cpg,gpc,"gpc")

def reset_bam(bam,genome_seq) :
    try : 
        refseq = bam.get_reference_sequence()
    except ValueError :
        try : 
            # MD tag not present in minimap2
            refseq = genome_seq[ 
                    bam.reference_start:
                    bam.reference_end]
        except :
            print("supply the reference genome (-f,--fasta)",file=sys.stderr)
            sys.exit()
    bam.query_sequence = refseq.upper()
    bam.cigarstring = ''.join([str(len(refseq)),"M"])
    return bam

def change_sequence(bam,calls,mod="cpg") :
    start = bam.reference_start
    pos = bam.get_reference_positions(True)
    if bam.is_reverse == True : 
        if mod == "cpg" :
            offset=1
            dinuc = "CN"
        elif mod == "gpc" :
            offset=-1
            dinuc = "NC"
        m="G"
        u="A"
    else : 
        if mod == "cpg" :
            offset=0
            dinuc = "NG"
        elif mod == "gpc" :
            offset = 0
            dinuc = "GN"
        m="C"
        u="T"
    if mod == "cpg" :
        seq = np.array(list(bam.query_sequence.replace("CG",dinuc)))
    elif mod == "gpc" :
        seq = np.array(list(bam.query_sequence.replace("GC",dinuc)))
#    seq[gsites-offset] = g
    if calls is not 0 :
        # methylated
        meth = calls[np.where(calls[:,1]==1),0]+offset
        seq[np.isin(pos,meth)] = m
        # unmethylated
        meth = calls[np.where(calls[:,1]==0),0]+offset
        seq[np.isin(pos,meth)] = u
    bam.query_sequence = ''.join(seq)
    return bam

def convertBam(bampath,genome_seq,cfunc,cpgpath,gpcpath,window,print_nometh,verbose,q) :
#    if verbose : print("reading {} from bam file".format(window),file=sys.stderr)
    with pysam.AlignmentFile(bampath,"rb") as bam :
        bam_entries = [x for x in bam.fetch(region=window)]
#    if verbose : print("{} reads in {}".format(len(bam_entries),window),file=sys.stderr)
    if len(bam_entries) == 0 : return
#    if verbose : print("reading {} from cpg data".format(window),file=sys.stderr)
    try : cpg_calldict = read_tabix(cpgpath,window)
    except ValueError :
        if verbose :
            print("No CpG methylation in {}, moving on".format(window),file=sys.stderr)
        return
#    if verbose : print("reading {} from gpc data".format(window),file=sys.stderr)
    try: gpc_calldict = read_tabix(gpcpath,window)
    except TypeError : gpc_calldict = cpg_calldict # no gpc provided, repace with cpg for quick fix
    except ValueError :
        if verbose :
            print("No GpC methylation in {}, moving on".format(window),file=sys.stderr)
        return
#    if verbose : print("converting {} reads in {}".format(len(bam_entries),window),file=sys.stderr)
    i = 0
    for bam in bam_entries :
        qname = bam.query_name
        try : cpg = cpg_calldict[qname]
        except KeyError : 
            cpg = 0
        try : gpc = gpc_calldict[qname]
        except KeyError : 
            gpc = 0
        i += 1
        newbam = reset_bam(bam,genome_seq)
        convertedbam = cfunc(newbam,cpg,gpc)
        if not print_nometh :
            if cpg is 0 or gpc is 0 :
                continue
        q.put(convertedbam.to_string())
    if verbose : print("converted {} bam entries in {}".format(i,window),file=sys.stderr)

def main() :
    args=parseArgs()
    sys.stderr = Unbuffered(sys.stderr)
    if args.window : 
        windows = [args.window]
    elif args.regions : 
        windows = [ bed_to_coord(x) for x in args.regions ]
    else :
        if args.verbose : 
            print("converting the whole genome",file=sys.stderr)
        windows = get_windows_from_bam(args.bam,100000)
    if args.verbose : print("{} regions to parse".format(len(windows)),file=sys.stderr)
    # read in fasta
    if args.fasta :
        fasta = pysam.FastaFile(args.fasta)
    # initialize mp
    manager = mp.Manager()
    q = manager.Queue()
    pool = mp.Pool(processes=args.threads)
    if args.verbose : print("using {} parallel processes".format(args.threads),file=sys.stderr)
    # watcher for output
    watcher = pool.apply_async(listener,(q,args.bam,args.out,args.verbose))
    # which convert function
    if args.gpc is None : converter = convert_cpg
    else : converter = convert_nome
    # start processing
    if args.fasta : 
        jobs = list()
        for win in windows : 
            chrom,start,end = coord_to_bed(win)
            seq = fasta.fetch(reference=chrom).upper()
            jobs.append(pool.apply_async(convertBam,
                args = (args.bam,seq,converter,args.cpg,args.gpc,
                    win,args.all,args.verbose,q)))
    else : 
        jobs = [ pool.apply_async(convertBam,
            args = (args.bam,0,converter,args.cpg,args.gpc,
                win,args.all,args.verbose,q))
            for win in windows ]
    output = [ p.get() for p in jobs ]
    # done
    q.put('kill')
    q.join()
    pool.close()
    if args.verbose : print("time elapsed : {} seconds".format(time.time()-start_time),file=sys.stderr)

if __name__=="__main__":
    main()

