from spacy.tokens import Span
import numpy as np
import operator
from medcat.utils.loggers import basic_logger
from medcat.utils.matutils import unitvec
import os
log = basic_logger("cat_spacycat")

# IF UMLS it includes specific rules that are only good for the Full UMLS version
if os.getenv('TYPE', 'other').lower() == 'umls':
    log.info("Using cat_ann for annotations")
    from medcat.cat_ann import CatAnn
else:
    log.info("Using basic_cat_ann for annotations")
    from medcat.basic_cat_ann import CatAnn



class SpacyCat(object):
    """ A Spacy pipe module, can be easily added into a spacey pipline

    cdb:  the cdb object of class cat.cdb representing the concepts
    vocab:  vocab object of class cat.utils.vocab with vector representations
    train:  should the training be performed or not, if training is False
            the disambiguation using vectors will be performed. While training is True
            it will not be performed
    """
    DEBUG = os.getenv('DEBUG', "false").lower() == 'true'
    NORM_EMB = os.getenv('NORM_EMB', "false").lower() == 'true' # Should we normalize the w2v
    PREFER_FREQUENT = os.getenv('PREFER_FREQUENT', "true").lower() == 'true'
    PREFER_ICD10 = os.getenv('PREFER_ICD10', "false").lower() == 'true'
    CNTX_SPAN = int(os.getenv('CNTX_SPAN', 9))
    CNTX_SPAN_SHORT = int(os.getenv('CNTX_SPAN_SHORT', 3))
    MIN_CUI_COUNT = int(os.getenv('MIN_CUI_COUNT', 30000))
    MIN_CUI_COUNT_STRICT = int(os.getenv('MIN_CUI_COUNT_STRICT', 5))
    # Just to be sure
    MIN_CUI_COUNT = max(MIN_CUI_COUNT_STRICT, MIN_CUI_COUNT)
    UPDATE_COO = os.getenv('UPDATE_COO', "false").lower() == 'true'
    ACC_ALWAYS = os.getenv('ACC_ALWAYS', "false").lower() == 'true'
    DISAMB_EVERYTHING = os.getenv('DISAMB_EVERYTHING', 'false').lower() == 'true'
    TUI_FILTER = os.getenv('TUI_FILTER', None)
    MAX_SKIP_TKN= int(os.getenv('MAX_SKIP_TKN', 1))
    SKIP_STOPWORDS = os.getenv('SKIP_STOPWORDS', "true").lower() == 'true'
    WEIGHTED_AVG = os.getenv('WEIGHTED_AVG', "true").lower() == 'true'

    MIN_ACC = float(os.getenv('MIN_ACC', 0.1))
    MIN_ACC_TH = float(os.getenv('MIN_ACC_TH', 0.1))
    MIN_CONCEPT_LENGTH = int(os.getenv('MIN_CONCEPT_LENGTH', 1))
    NEG_PROB = float(os.getenv('NEG_PROB', 0.5))
    LBL_STYLE = os.getenv('LBL_STYLE', 'long').lower()

    LR = float(os.getenv('LR', 1))
    ANNEAL = os.getenv('ANNEAL', 'true').lower() == 'true'


    def __init__(self, cdb, vocab=None, train=False, force_train=False, tokenizer=None):
        self.cdb = cdb
        self.vocab = vocab
        self.train = train
        self.cat_ann = CatAnn(self.cdb, self)
        self._train_skip_names = {}
        self.force_train = force_train

        if self.vocab is None:
            self.vocab = self.cdb.vocab

        if tokenizer is None:
            self.tokenizer = self._tok
        else:
            self.tokenizer = tokenizer

        # Weight drops for average
        self.wdrops = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2] + [0.1] * 300


    def _tok(self, text):
        return [text]


    def _get_doc_words(self, doc, tkns, span, skip_current=False, skip_words=False):
        """ Get words around a certain token

        doc:  spacy document
        tkns:  tokens around which we want to find the words
        span:  window size
        skip_current:  if True found words will not include the current tkns
        skip_words:  If True stopwords and punct will be skipped
        """
        weights = []
        words = []

        # Go left
        i = tkns[0].i - 1
        n = 0
        add_weight = True
        while(n < span and i >= 0):
            word = doc[i]
            if self.NORM_EMB:
                _word = word._.norm
            else:
                _word = word.text.lower()

            if self.WEIGHTED_AVG:
                if add_weight:
                    weights = [self.wdrops[n]] + weights
                    add_weight = False
            else:
                weights.append(1.0)

            if skip_words:
                if not word._.to_skip and not word.is_digit:
                    words = self.tokenizer(_word) + words

                    n += 1
                    add_weight = True
            else:
                words = self.tokenizer(_word) + words

                n += 1
                add_weight = True

            i = i - 1

        # Add tokens if not skip_current
        if not skip_current:
            for tkn in tkns:
                words.append(tkn._.norm)
                weights.append(1.0)

        # Go right
        i = tkns[-1].i + 1
        n = 0
        add_weight = True
        while(n < span and i < len(doc)):
            word = doc[i]
            if self.NORM_EMB:
                _word = word._.norm
            else:
                _word = word.text.lower()

            if self.WEIGHTED_AVG:
                if add_weight:
                    weights.append(self.wdrops[n])
                    add_weight = False
            else:
                weights.append(1.0)

            if skip_words:
                if not word._.to_skip and not word.is_digit:
                    words = words + self.tokenizer(_word)
                    n += 1
                    add_weight = True
            else:
                words = words + self.tokenizer(_word)
                n += 1
                add_weight = True

            i = i + 1

        return words, weights


    def _calc_acc(self, cui, doc, tkns, name=None):
        """ Calculate the accuracy for an annotation

        cui:  concept id
        doc:  spacy document
        tkns:  tokens for the concept that was found
        name:  concept name
        """
        cntx = None
        cntx_short = None
        words, weights = self._get_doc_words(doc, tkns, span=self.CNTX_SPAN, skip_words=True, skip_current=False)
        words_short, weights_short = self._get_doc_words(doc, tkns, span=self.CNTX_SPAN_SHORT, skip_current=True)

        cntx_vecs = []
        for w_ind, word in enumerate(words):
            if word in self.vocab and self.vocab.vec(word) is not None:
                cntx_vecs.append(self.vocab.vec(word) * weights[w_ind])

        cntx_vecs_short = []
        for w_ind, word in enumerate(words_short):
            if word in self.vocab and self.vocab.vec(word) is not None:
                cntx_vecs_short.append(self.vocab.vec(word) * weights_short[w_ind])

        if len(cntx_vecs_short) > 0:
            cntx_short = np.average(cntx_vecs_short, axis=0)

        if len(cntx_vecs) > 0:
            cntx = np.average(cntx_vecs, axis=0)

        #### DEBUG ONLY ####
        if self.DEBUG:
            if cui in self.cdb.cui2context_vec and len(cntx_vecs) > 0:
                log.debug("SIMILARITY MED::::::::::::::::::::")
                log.debug(words)
                log.debug(cui)
                log.debug(tkns)
                log.debug(np.dot(unitvec(cntx),
                          unitvec(self.cdb.cui2context_vec[cui])))
                log.debug(":::::::::::::::::::::::::::::::::::\n")

            if cui in self.cdb.cui2context_vec_short and len(cntx_vecs_short) > 0:
                log.debug("SIMILARITY SHORT::::::::::::::::::::")
                log.debug(words_short)
                log.debug(cui)
                log.debug(tkns)
                log.debug(np.dot(unitvec(cntx_short),
                          unitvec(self.cdb.cui2context_vec_short[cui])))
                log.debug(":::::::::::::::::::::::::::::::::::\n")
        #### END OF DEBUG ####

        if cui in self.cdb.cui2context_vec and len(cntx_vecs) > 0:
            sim = np.dot(unitvec(cntx), unitvec(self.cdb.cui2context_vec[cui]))

            if cui in self.cdb.cui2context_vec_short and len(cntx_vecs_short) > 0:
                sim2 = np.dot(unitvec(cntx_short), unitvec(self.cdb.cui2context_vec_short[cui]))
                if sim2 > 0 and abs(sim - sim2) > 0.1:
                    sim = (sim + sim2) / 2
            if name is not None:
                if cui in self.cdb.cui2pref_name:
                    if name == self.cdb.cui2pref_name[cui]:
                        sim = min(1, sim + 0.1)
            return sim
        else:
            return -1


    def add_ncntx_vec(self, cui, words):
        cntx_vecs = []
        for word in words:
            tmp = self.tokenizer(word.lower())
            for w in tmp:
                if w in self.vocab and self.vocab.vec(w) is not None:
                    cntx_vecs.append(self.vocab.vec(w))

        cntx = np.average(cntx_vecs, axis=0)

        self.cdb.add_ncontext_vec(cui, cntx)


    def _add_cntx_vec(self, cui, doc, tkns, negative=False, lr=None, anneal=None):
        """ Add context vectors for this CUI

        cui:  concept id
        doc:  spacy document where the cui was found
        tkns:  tokens that were found for this cui
        """
        if lr is None:
            lr = self.LR
        if anneal is None:
            anneal = self.ANNEAL

        if negative:
            self.cdb.cui_disamb_always[cui] = True

        # Get words around this concept
        words, weights = self._get_doc_words(doc, tkns, span=self.CNTX_SPAN, skip_words=True, skip_current=False)
        words_short, weights_short = self._get_doc_words(doc, tkns, span=self.CNTX_SPAN_SHORT, skip_current=True)

        cntx_vecs = []
        for w_ind, word in enumerate(words):
            if word in self.vocab and self.vocab.vec(word) is not None:
                cntx_vecs.append(self.vocab.vec(word) * weights[w_ind])

        cntx_vecs_short = []
        for w_ind, word in enumerate(words_short):
            if word in self.vocab and self.vocab.vec(word) is not None:
                cntx_vecs_short.append(self.vocab.vec(word) * weights_short[w_ind])

        if len(cntx_vecs) > 0:
            cntx = np.average(cntx_vecs, axis=0)
            # Add context vectors only if we have some
            self.cdb.add_context_vec(cui, cntx, cntx_type='MED', negative=negative, lr=lr,
                                     anneal=anneal)

        if len(cntx_vecs_short) > 0:
            cntx_short = np.average(cntx_vecs_short, axis=0)
            # Add context vectors only if we have some
            self.cdb.add_context_vec(cui, cntx_short, cntx_type='SHORT', inc_cui_count=False,
                    negative=negative, lr=lr, anneal=anneal)

        if np.random.rand() < self.NEG_PROB and not negative:
            # Add only if probability and 'not' negative input
            negs = self.vocab.get_negative_samples(n=self.CNTX_SPAN * 2)
            neg_cntx_vecs = [self.vocab.vec(self.vocab.index2word[x]) for x in negs]
            neg_cntx = np.average(neg_cntx_vecs, axis=0)
            self.cdb.add_context_vec(cui, neg_cntx, negative=True, cntx_type='MED',
                                      inc_cui_count=False, lr=lr, anneal=True)

        #### DEBUG ONLY ####
        if self.DEBUG:
            if cui in self.cdb.cui2context_vec and len(cntx_vecs) > 0:
                if np.dot(unitvec(cntx), unitvec(self.cdb.cui2context_vec[cui])) < 0.01:
                    log.debug("SIMILARITY MED::::::::::::::::::::")
                    log.debug(words)
                    log.debug(cui)
                    log.debug(tkns)
                    log.debug(np.dot(unitvec(cntx),
                              unitvec(self.cdb.cui2context_vec[cui])))
                    log.debug(":::::::::::::::::::::::::::::::::::\n")

            if cui in self.cdb.cui2context_vec_short and len(cntx_vecs_short) > 0:
                if np.dot(unitvec(cntx_short), unitvec(self.cdb.cui2context_vec_short[cui])) < 0.01:
                    log.debug("SIMILARITY SHORT::::::::::::::::::::")
                    log.debug(words_short)
                    log.debug(cui)
                    log.debug(tkns)
                    log.debug(np.dot(unitvec(cntx_short),
                              unitvec(self.cdb.cui2context_vec[cui])))
                    log.debug(":::::::::::::::::::::::::::::::::::\n")


    def _add_ann(self, cui, doc, tkns, acc=-1, name=None, is_disamb=False):
        """ Add annotation to a document

        cui:  concept id
        doc:  spacy document where the concept was found
        tkns:  tokens for this cui
        acc:  accuracy for this annotation
        name:  concept name
        """
        # Skip if tui filter
        if self.TUI_FILTER is None or self.cdb.cui2tui[cui] in self.TUI_FILTER:
            if not is_disamb and cui in self.cdb.cui_disamb_always:
                self.to_disamb.append((list(tkns), name))
            else:
                if self.LBL_STYLE == 'long':
                    lbl = "{} - {} - {} - {} - {:.2}".format(
                            cui,
                            self.cdb.cui2pretty_name.get(cui, ''),
                            self.cdb.cui2tui.get(cui, ''),
                            self.cdb.tui2name.get(self.cdb.cui2tui.get(cui, ''), ''),
                            float(acc))
                elif self.LBL_STYLE == 'ent':
                    lbl = "{} - {:.2}".format(self.cdb.tui2name.get(
                        self.cdb.cui2tui.get(cui, ''), ''),
                        float(acc))
                else:
                    lbl = cui

                lbl = doc.vocab.strings.add(lbl)
                ent = Span(doc, tkns[0].i, tkns[-1].i + 1, label=lbl)


                if self.ACC_ALWAYS:
                    acc = self._calc_acc(cui, doc, tkns, name)

                ent._.acc = acc
                ent._.cui = cui
                ent._.tui = self.cdb.cui2tui.get(cui, 'None')
                ent._.id = self.ent_id
                self.ent_id += 1
                doc._.ents.append(ent)

                # Increase counter for cui_count_ext if not already added
                if cui not in self._cuis:
                    if cui in self.cdb.cui_count_ext:
                        self.cdb.cui_count_ext[cui] += 1
                    else:
                        self.cdb.cui_count_ext[cui] = 1

                if self.train or self.force_train:
                    self._add_cntx_vec(cui, doc, tkns)

                self._cuis.add(cui)


    def _create_main_ann(self, doc, tuis=None):
        """ Only used for testing. Creates annotation in the spacy ents list
        from all the annotations for this document.

        doc:  spacy document
        """
        doc._.ents.sort(key=lambda x: len(x.text), reverse=True)

        tkns_in = []
        for ent in doc._.ents:
            if tuis is None or ent._.tui in tuis:
                to_add = True
                for tkn in ent:
                    if tkn in tkns_in:
                        to_add = False
                if to_add:
                    for tkn in ent:
                        tkns_in.append(tkn)
                    doc.ents = list(doc.ents) + [ent]


    def __call__(self, doc):
        """ Annotate a document

        doc:  spacy document
        """
        self.ent_id = 0
        self._cuis = set()
        doc._.ents = []
        # Get the words in this document that should not be skipped
        doc_words = [t._.norm for t in doc if not t._.to_skip]

        _doc = []
        for token in doc:
            if not token._.to_skip or token.is_stop:
                _doc.append(token)

        self.to_disamb = []
        for i in range(len(_doc)):
            # Go through all the tokens in this document and annotate them
            tkns = [_doc[i]]
            name = _doc[i]._.norm
            raw_name = _doc[i].lower_


            if raw_name in self.cdb.name2cui and len(raw_name) > self.MIN_CONCEPT_LENGTH:
                # Add annotation
                if not self.train or not self._train_skip(raw_name) or self.force_train:
                    if not _doc[i].is_stop:
                        if self.DISAMB_EVERYTHING:
                            self.to_disamb.append((list(tkns), raw_name))
                        else:
                            self.cat_ann.add_ann(raw_name, tkns, doc, self.to_disamb, doc_words)
            elif name in self.cdb.name2cui and len(name) > self.MIN_CONCEPT_LENGTH:
                # Add annotation
                if not self.train or not self._train_skip(name) or self.force_train:
                    if not _doc[i].is_stop:
                        if self.DISAMB_EVERYTHING:
                            self.to_disamb.append((list(tkns), name))
                        else:
                            self.cat_ann.add_ann(name, tkns, doc, self.to_disamb, doc_words)

            last_notskipped_tkn = tkns[-1]
            for j in range(i+1, len(_doc)):
                # Don't allow more than MAX skipped tokens
                if _doc[j].i - last_notskipped_tkn.i - 1 > self.MAX_SKIP_TKN:
                    break

                skip = False
                if _doc[j].is_stop and self.SKIP_STOPWORDS:
                    # If it is a stopword, skip for name
                    skip = True
                else:
                    # Add to name only the ones that are not skipped
                    name = name + _doc[j]._.norm
                    last_notskipped_tkn = _doc[j]

                raw_name = raw_name + _doc[j].lower_
                tkns.append(_doc[j])

                if name not in self.cdb.sname2name and raw_name not in self.cdb.sname2name:
                    # There is not one entity containing these words
                    break
                else:
                    if raw_name in self.cdb.name2cui and len(raw_name) > self.MIN_CONCEPT_LENGTH:
                        if not self.train or not self._train_skip(raw_name) or self.force_train:
                            if self.DISAMB_EVERYTHING:
                                self.to_disamb.append((list(tkns), raw_name))
                            else:
                                self.cat_ann.add_ann(raw_name, tkns, doc, self.to_disamb, doc_words)
                    elif not skip and name in self.cdb.name2cui and len(name) > self.MIN_CONCEPT_LENGTH:
                        if not self.train or not self._train_skip(name) or self.force_train:
                            if self.DISAMB_EVERYTHING:
                                self.to_disamb.append((list(tkns), name))
                            else:
                                self.cat_ann.add_ann(name, tkns, doc, self.to_disamb, doc_words)



        if not self.train or self.force_train:
            self.disambiguate(self.to_disamb)

        # Add coocurances
        if not self.train and self.UPDATE_COO:
            self.cdb.add_coos(list(self._cuis))

        # Create main annotations
        self._create_main_ann(doc)

        return doc


    def _train_skip(self, name):
        # Down sampling of frequent terms
        cnt_mult = 1
        if name not in self._train_skip_names:
            self._train_skip_names[name] = 1

        cnt = self._train_skip_names[name]
        if cnt < self.MIN_CUI_COUNT * cnt_mult:
            self._train_skip_names[name] += 1
            return False

        cnt = cnt - (self.MIN_CUI_COUNT * cnt_mult) + 1
        prob = 1 / cnt
        p = np.random.rand()
        if p < prob:
            self._train_skip_names[name] += 1
            return False
        else:
            return True


    def disambiguate(self, to_disamb):
        # Do vector disambiguation only if not training
        log.debug("?"*100)
        log.debug("There are {} concepts to be disambiguated.".format(len(to_disamb)))
        log.debug("The concepts are: " + str(to_disamb))


        for concept in to_disamb:
            # Loop over all concepts to be disambiguated
            tkns = concept[0]
            name = concept[1]
            cuis = list(self.cdb.name2cui[name])

            # Remove cuis if tui filter
            """
            if self.TUI_FILTER is not None:
                new_cuis = []
                for cui in cuis:
                    if self.cdb.cui2tui[cui] in self.TUI_FILTER:
                        new_cuis.append(cui)
                cuis = new_cuis
            """

            if len(cuis) > 0:
                if len(cuis) > 1:
                    _min_acc = self.MIN_ACC
                else:
                    _min_acc = self.MIN_ACC_TH

                accs = []
                cnts = []
                MIN_COUNT = self.MIN_CUI_COUNT_STRICT
                for cui in cuis:
                    # Each concept can have one or more cuis assigned
                    if self.cdb.cui_count.get(cui, 0) >= MIN_COUNT:
                        # If this cui appeared enough times
                        accs.append(self._calc_acc(cui, tkns[0].doc, tkns, name))
                        cnts.append(self.cdb.cui_count.get(cui, 0))
                    else:
                        # If not just set the accuracy to -1
                        accs.append(-1)
                        cnts.append(-1)

                # TODO: SEPARATE INTO A CLASS THAT MODIFIES THE SCORES
                if self.PREFER_FREQUENT and cnts and max(cnts) > 100:
                    # Prefer frequent concepts, only in cases when cnt > 100
                    mps = np.array([1] * len(cnts), dtype=np.float)
                    _cnts = np.array(cnts)
                    mps[np.where(_cnts < (max(cnts) / 2))] = 0.9
                    mps[np.where(_cnts < (max(cnts) / 10))] = 0.8
                    mps[np.where(_cnts < (max(cnts) / 50))] = 0.7

                    accs = accs * mps

                if self.PREFER_ICD10:
                    # Prefer concepts that have ICD10
                    mps = np.array([1] * len(cnts), dtype=np.float)
                    noicd = [False if 'icd10' in self.cdb.cui2info.get(cui, {}) else
                             True for cui in cuis]
                    mps[noicd] = 0.5
                    accs = accs * mps

                ind = np.argmax(accs)
                cui = cuis[ind]
                acc = accs[ind]
                # Add only if acc > _min_acc 
                if acc > _min_acc:
                    self._add_ann(cui, tkns[0].doc, tkns, acc, is_disamb=True)
