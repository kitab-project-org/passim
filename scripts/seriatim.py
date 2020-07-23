import argparse
import heapq as hq
import json, os, sys
from math import ceil, log, inf
from pyspark.sql import SparkSession, Row
from pyspark.sql.functions import col, explode, size, udf, struct, length, collect_list, collect_set, sort_array, when, expr, explode, slice, map_from_entries, flatten, xxhash64, monotonically_increasing_id, lit, array, arrays_zip
from pyspark.sql.types import *

from dataclasses import dataclass

def getPostings(text, n, floating_ngrams):
    tf = dict()
    posts = list()
    for i, c in enumerate(text):
        if c.isalnum() and ( floating_ngrams or i == 0 or not text[i-1].isalnum() ):
            j = i + 1
            buf = ''
            while j < len(text) and len(buf) < n:
                if text[j].isalnum():
                    buf += text[j].lower()
                j += 1
            if len(buf) >= n:
                tf[buf] = tf.get(buf, 0) + 1
                posts.append((buf, i))
    return [(key, tf[key], i) for key, i in posts]

def vitSrc(pos, n, max_gap, min_align, min_match):
    @dataclass(frozen=True)
    class BP:
        lab: int
        pos2: int
        pos: int

    @dataclass(frozen=True)
    class Score:
        pos2: int
        pos: int
        score: float

    doc = dict()
    for p in pos:
        for m in p.alg:
            doc[m.uid] = doc.get(m.uid, 0) + 1

    ## b70 docs: 21918104; 237921873725 characters
    N = 100 # 21918104
    pcopy = 0.8
    pcont = 0.998
    V = 256
    lpcopy = log(pcopy)
    lpmiss = log(1 - pcopy) - log(2 * V)
    lpstop = log((1 + pcopy) / 2)
    lpcont = log(pcont)
    lpswitch = log(1 - pcont)

    ## Spans can only begin or end at matches
    ## Should probably just use start points in dynamic program
    bp = dict()
    prev = {}
    bgscore = [Score(0, 0, 0.0)]
    last = 0
    for p in pos:
        npos = p.post2
        stride = npos - last
        i = len(bgscore) - 1
        while i >= 0 and bgscore[i].pos2 > npos:
            i -= 1
        bglast = bgscore[i]
        bg = bglast.score + (p.post2 - bglast.pos2) * (lpcont + -log(V))
        # prev[0] = Score(npos + n, 0, bg + log(p.df) - log(N))
        prev[0] = Score(npos + n, 0, bg + n * (lpcont + -log(V)))
        bp[BP(0, npos + n, 0)] = BP(0, bglast.pos2, 0)
        # print(prev[0], file=sys.stderr)
        for m in p.alg:
            if doc.get(m.uid, 0) < min_match:
                continue
            same = prev.get(m.uid, Score(0, 0, -inf))
            gap = max(0, m.post - same.pos - n)
            gap2 = max(0, p.post2 - same.pos2 - n)
            # Show we treat gaps on the source side the same? What about retrogrades?
            overlap = min(gap, gap2)
            minsub = ceil(overlap / n)
            minerr = minsub + gap2 - overlap #abs(gap2 - gap)
            cont = same.score + gap2 * lpcont + minerr * lpmiss + (overlap - minsub) * lpcopy \
                + min(n, npos - same.pos2) * (lpcont + lpcopy)
            switch = bg + lpswitch + log(0.001) + n * (lpcont + lpcopy)
            if (gap2 > max_gap or gap > max_gap or m.post < same.pos or switch > cont) and stride > n:
                score = switch
                bp[BP(m.uid, npos, m.post)] = BP(0, bglast.pos2, 0)
            else:
                score = cont
                bp[BP(m.uid, npos, m.post)] = BP(m.uid, same.pos2, same.pos)
            prev[m.uid] = Score(npos, m.post, score)
            stop = score + lpstop + lpswitch
            if stop > prev[0].score:
                prev[0] = Score(npos + n, 0, stop)
                # print("# Stop %d @ %d: " % (m.uid, npos) + str(prev), file=sys.stderr)
                bp[BP(0, npos + n, 0)] = BP(m.uid, npos, m.post)
        last = npos
        bgscore.append(prev[0])
        # print("%d: " % npos, prev, file=sys.stderr)
        # print(bp, file=sys.stderr)

    matches = list()
    cur = BP(0, last + n, 0)
    while cur.pos2 > 0:
        matches.append(cur)
        cur = bp[cur]
    matches.reverse()

    # return [(m.lab, m.pos2, m.pos2 + n, m.pos, m.pos + n) for m in matches]

    ## Merge matches into spans
    spans = list()
    i = 0
    while i < len(matches):
        if matches[i].lab != 0:
            start = matches[i]
            j = i
            while j < len(matches):
                if matches[j].lab == 0:
                    if False and ((j+1) < len(matches) and matches[j+1].lab == matches[i].lab and
                        ## Allow singleton retrograde alignments
                        (matches[j+1].pos >= matches[i].pos
                         and (matches[j+1].pos2 - matches[j].pos2) < max_gap)):
                        j += 1
                    else:
                        if (matches[j-1].pos2 - matches[i].pos2 + n) >= min_align:
                            spans.append((matches[i].lab, matches[i].pos2, matches[j-1].pos2 + n,
                                          matches[i].pos, matches[j-1].pos + n))
                        break
                j += 1
            i = j
        i += 1

    return spans

def spanEdge(src, max_gap):
    res = list()
    for i, s in enumerate(src):
        lend = src[i-1].end2 if i > 0 else 0
        left2 = max(lend, s.begin2 - max_gap)
        rend = src[i+1].begin2 if (i + 1) < len(src) else s.end2 + max_gap
        right2 = min(rend, s.end2 + max_gap)
        left = max(0, s.begin - (s.begin2 - left2 + 10))
        right = s.end + 2 * (right2 - s.end2)
        res.append((s.uid, left2, s.begin2, s.end2, right2, left, s.begin, s.end, right))
    return res

def anchorAlign(s1, s2, side):
    if side == 'left':
        s1 = s1[::-1]
        s2 = s2[::-1]

    width = 10
    V = 256
    logV = log(V)
    pcopy = 0.8
    lpcopy = log(pcopy)
    lpedit = log(1 - pcopy) - log(2 * V)
    lpstop = log((1 + pcopy) / 2)
    pfinal = 0.01
    lpfinal = log(pfinal)
    lppad = log(1 - pfinal) - log(V)

    #print(len(s1), s1)
    #print(len(s2), s2)
    #return "done!"

    chart = {}
    pq = [(0, (0, 0, 0, 0))]
    while len(pq) > 0:
        #print(pq)
        top = hq.heappop(pq)
        #print(top)
        (score, item) = top
        (s, t, e1, e2) = item
        if s == len(s1) and t == len(s2):
            break
        ## Finish
        cand = [(score - (lpstop + 2 * lpfinal + lppad * (len(s2) - t)),
                  (len(s1), len(s2), s, t))]
        if s < len(s1):         # delete
            cand.append((score - lpedit, (s + 1, t, e1, e2)))
            if t < len(s2):
                if s1[s].lower() == s2[t].lower() or (s1[s].isspace() and s2[t].isspace()): #copy
                    cand.append((score - lpcopy, (s + 1, t + 1, e1, e2)))
                else:
                    cand.append((score - lpedit, (s + 1, t + 1, e1, e2)))
        if t < len(s2):         # insert
            cand.append((score - lpedit, (s, t + 1, e1, e1)))
        for c in cand:
            (score, item) = c
            if item[2] == 0 and abs(item[0] - item[1]) > width:
                continue
            if chart.get(item, inf) > score:
                chart[item] = score
                hq.heappush(pq, c)
                
    (score, (s, t, e1, e2)) = top
    # It might be worth deleting small numbers of characters trailing (leading) a newline.
    if side == 'left':
        return (s1[e1-1::-1], s2[e2-1::-1])
    else:
        return (s1[0:e1], s2[0:e2])

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Passim Alignment',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-i', '--id', type=str, default='id',
                        help='Field for unique document IDs')
    parser.add_argument('-t', '--text', type=str, default='text',
                        help='Field for document text')
    parser.add_argument('-l', '--minDF', type=int, default=2,
                        help='Lower limit on document frequency', metavar='N')
    parser.add_argument('-u', '--maxDF', type=int, default=100,
                        help='Upper limit on document frequency', metavar='N')
    parser.add_argument('-m', '--min-match', type=int, metavar='N', default=5,
                        help='Minimum number of n-gram matches between documents')
    parser.add_argument('-n', '--n', type=int, default=20,
                        help='n-gram order', metavar='N')
    parser.add_argument('--floating-ngrams', action='store_true',
                        help='Allow n-grams to float from word boundaries')
    parser.add_argument('-g', '--gap', type=int, default=600,
                        help='Minimum size of gap that separates passages', metavar='N')
    parser.add_argument('-a', '--min-align', type=int, default=50,
                         help='Minimum length of alignment', metavar='N')
    parser.add_argument('--fields', type=str, nargs='+', default=[],
                        help='List of fileds to index')
    parser.add_argument('-f', '--filterpairs', type=str, default='uid < uid2',
                        help='SQL constraint on posting pairs; default=uid < uid2')
    parser.add_argument('--input-format', type=str, default='json',
                        help='Input format')
    parser.add_argument('--output-format', type=str, default='json',
                        help='Output format')
    parser.add_argument('inputPath', metavar='<path>', help='input data')
    parser.add_argument('outputPath', metavar='<path>', help='output')
    config = parser.parse_args()

    print(config)

    spark = SparkSession.builder.appName('Passim Alignment').getOrCreate()

    dfpostFname = os.path.join(config.outputPath, 'dfpost.parquet')
    pairsFname = os.path.join(config.outputPath, 'pairs.parquet')
    srcFname = os.path.join(config.outputPath, 'src.parquet')
    outFname = os.path.join(config.outputPath, 'out.' + config.output_format)

    corpus = spark.read.option('mergeSchema',
                               'true').format(config.input_format).load(config.inputPath
                               ).na.drop(subset=[config.id, config.text]
                               ).withColumn('uid', xxhash64(config.id))

    termCorpus = corpus.selectExpr('uid', config.text, *config.fields)

    spark.conf.set('spark.sql.shuffle.partitions', corpus.rdd.getNumPartitions() * 3)

    get_postings = udf(lambda text: getPostings(text, config.n, config.floating_ngrams),
                       ArrayType(StructType([
                           StructField('feat', StringType()),
                           StructField('tf', IntegerType()),
                           StructField('post', IntegerType())])))

    posts = termCorpus.withColumn('post', explode(get_postings(config.text))
                     ).select(*[col(f) for f in termCorpus.columns], col('post.*')
                     ).drop(config.text
                     ).withColumn('feat', xxhash64('feat')
                     ).filter(col('tf') == 1
                     ).drop('tf')

    df = posts.groupBy('feat').count().select('feat', col('count').cast('int').alias('df')
             ).filter( (col('df') >= config.minDF) & (col('df') <= config.maxDF) )

    posts.join(df, 'feat').write.mode('ignore').save(dfpostFname)
    
    dfpost = spark.read.load(dfpostFname)

    f1 = [f for f in dfpost.columns if f not in ['feat', 'df', 'post']]
    f2 = [f + '2' for f in f1]

    spark.conf.set('spark.sql.mapKeyDedupPolicy', 'LAST_WIN')
    
    dfpost.join(dfpost.toDF(*[f + ('2' if f != 'feat' else '') for f in dfpost.columns]),
                'feat'
         ).filter(config.filterpairs
         ).drop('feat', 'df2'
         ).groupBy(*f2, *f1
         ).agg(collect_list(struct('post', 'post2', 'df')).alias('plist')
         ).filter(size('plist') >= config.min_match
         ).withColumn('post', explode('plist')
         ).select(*f2, *f1, col('post.*')
         ).groupBy(*f2, 'post2', 'df'
         ).agg(collect_list(struct('uid', 'post')).alias('alg'),
               collect_set(struct('uid', struct(*[f for f in f1 if f != 'uid']))).alias('meta')
         ).groupBy(*f2
         ).agg(sort_array(collect_list(struct('post2', 'df', 'alg'))).alias('post'),
               map_from_entries(flatten(collect_set('meta'))).alias('meta')
         ).write.mode('ignore').parquet(pairsFname)
    
    pairs = spark.read.load(pairsFname)
    
    vit_src = udf(lambda post: vitSrc(post,
                                      config.n, config.gap, config.min_align, config.min_match),
                  ArrayType(StructType([
                      StructField('uid', LongType()),
                      StructField('begin2', IntegerType()),
                      StructField('end2', IntegerType()),
                      StructField('begin', IntegerType()),
                      StructField('end', IntegerType())])))

    pairs.withColumn('src', vit_src('post')).write.mode('ignore').parquet(srcFname)

    srcmap = spark.read.load(srcFname)

    # f2 = [f for f in srcmap.columns if f.endswith('2')]
    # f1 = [f.replace('2', '') for f in f2]

    span_edge = udf(lambda src: spanEdge(src, 200), # config.gap
                    ArrayType(StructType([
                        StructField('uid', LongType()),
                        StructField('left2', IntegerType()),
                        StructField('begin2', IntegerType()),
                        StructField('end2', IntegerType()),
                        StructField('right2', IntegerType()),
                        StructField('left', IntegerType()),
                        StructField('begin', IntegerType()),
                        StructField('end', IntegerType()),
                        StructField('right', IntegerType())])))

    anchor_align = udf(lambda s1, s2, side: anchorAlign(s1, s2, side),
                       StructType([
                           StructField('s1', StringType()),
                           StructField('s2', StringType())]))

    # We align edges independently, but we could also consider
    # aligning gaps between target spans jointly so that they don't
    # overlap.
    srcmap.withColumn('src', arrays_zip(span_edge('src'),
                                        expr('transform(src, s -> meta[s.uid])'))
         ).drop('post', 'meta', 'info'
         ).select(*f2, explode('src').alias('src')
         ).select(*f2, col('src.0.*'), col('src.1.*')
         ).join(termCorpus.select('uid', col(config.text).alias('text')), 'uid'
         ).withColumn('prefix', col('text').substr(col('left') + 1, col('begin') - col('left'))
         ).withColumn('suffix', col('text').substr(col('end') - config.n + 1,
                                                   col('right') - col('end') + config.n)
         ).withColumn('text', col('text').substr(col('begin') + 1,
                                                 col('end') - col('begin') - config.n)
         ).join(termCorpus.select(col('uid').alias('uid2'), col(config.text).alias('text2')),
                'uid2'
         ).withColumn('prefix2', col('text2').substr(col('left2') + 1, col('begin2') - col('left2'))
         ).withColumn('suffix2', col('text2').substr(col('end2') - config.n + 1,
                                                     col('right2') - col('end2') + config.n)
         ).withColumn('text2',
                      col('text2').substr(col('begin2') + 1, col('end2') - col('begin2') - config.n)
         ).withColumn('lalg', anchor_align('prefix', 'prefix2', lit('left'))
         ).withColumn('ralg', anchor_align('suffix', 'suffix2', lit('right'))
         ).write.json(outFname)

    spark.stop()
