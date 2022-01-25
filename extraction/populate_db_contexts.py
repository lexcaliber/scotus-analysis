import re

from db.sqlalchemy.models import Opinion, Cluster, OpinionParenthetical, CitationContext
from bs4 import BeautifulSoup
from sqlalchemy import select
from utils.format import format_reporter
import eyecite
from eyecite.models import CaseCitation
from eyecite.tokenizers import Tokenizer, AhocorasickTokenizer
from string import ascii_lowercase

from utils.logger import Logger

STOP_WORDS = {'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 'any', 'are', "aren't",
              'as', 'at', 'be', 'because', 'been', 'before', 'being', 'below', 'between', 'both', 'but', 'by', "can't",
              'cannot', 'could', "couldn't", 'did', "didn't", 'do', 'does', "doesn't", 'doing', "don't", 'down',
              'during', 'each', 'few', 'for', 'from', 'further', 'had', "hadn't", 'has', "hasn't", 'have', "haven't",
              'having', 'he', "he'd", "he'll", "he's", 'her', 'here', "here's", 'hers', 'herself', 'him', 'himself',
              'his', 'how', "how's", 'i', "i'd", "i'll", "i'm", "i've", 'if', 'in', 'into', 'is', "isn't", 'it', "it's",
              'its', 'itself', "let's", 'me', 'more', 'most', "mustn't", 'my', 'myself', 'no', 'nor', 'not', 'of',
              'off', 'on', 'once', 'only', 'or', 'other', 'ought', 'our', 'ours', 'ourselves', 'out', 'over', 'own',
              'same', "shan't", 'she', "she'd", "she'll", "she's", 'should', "shouldn't", 'so', 'some', 'such', 'than',
              'that', "that's", 'the', 'their', 'theirs', 'them', 'themselves', 'then', 'there', "there's", 'these',
              'they', "they'd", "they'll", "they're", "they've", 'this', 'those', 'through', 'to', 'too', 'under',
              'until', 'up', 'very', 'was', "wasn't", 'we', "we'd", "we'll", "we're", "we've", 'were', "weren't",
              'what', "what's", 'when', "when's", 'where', "where's", 'which', 'while', 'who', "who's", 'whom', 'why',
              "why's", 'with', "won't", 'would', "wouldn't", 'you', "you'd", "you'll", "you're", "you've", 'your',
              'yours', 'yourself', 'yourselves', ' ', 'court', "court's"}
LETTERS = set(ascii_lowercase)
PARENTHETICAL_BLACKLIST_REGEX = re.compile(
    r"^\s*(?:(?:(?:(?:majority|concurring|dissenting|in chambers|for the Court)(?: (in part|in judgment|in judgment in part|in result)?)(?: and (?:(?:concurring|dissenting|in chambers|for the Court)(?: (in part|in judgment|in judgment in part|in result)?)?)?)?) opinion)|(?:(?:(?:majority|concurring|dissenting|in chambers|for the Court)(?: (in part|in judgment|in judgment in part|in result)?)(?: and (?:(?:concurring|dissenting|in chambers|for the Court)(?: (in part|in judgment|in judgment in part|in result))?)?)?)? ?opinion of \S+ (?:J.|C.\s*J.))|(?:(?:quoting|citing).*)|(?:per curiam)|(?:(?:plurality|majority|dissenting|concurring)(?: (?:opinion|statement))?)|(?:\S+,\s*(?:J.|C.\s*J.(?:, joined by .*,)?)(?:, (?:(?:concurring|dissenting|in chambers|for the Court)(?: (in part|in judgment|in judgment in part|in result|(?:from|with|respecting) ?denial of certiorari)?)?(?: and (?:(?:concurring|dissenting|in chambers|for the Court)(?: (in part|in(?: the)? judgment|in judgment in part|in result|(?:from|with|respecting) denial of certiorari))?)?)?))?)|(?:(?:some )?(?:internal )?(?:brackets?|footnotes?|alterations?|quotations?|quotation marks?|citations?|emphasis)(?: and (?:brackets?|footnotes?|alterations?|quotations?|quotation marks?|citations?|emphasis))? (?:added|omitted|deleted|in original|altered|modified))|(?:same|similar)|(?:slip op.* \d*)|denying certiorari|\w+(?: I{1,3})?|opinion in chambers|opinion of .*)\s*$")


class OneTimeTokenizer(Tokenizer):
    """
    Wrap the CourtListener tokenizer to save tokenization results.
    """

    def __init__(self):
        self.words = []
        self.cit_toks = []

    def tokenize(self, text: str):
        if not self.words or self.cit_toks:
            # some of the static methods in AhocorasickTokenizer don't like children.
            self.words, self.cit_toks = AhocorasickTokenizer().tokenize(text)
        return self.words, self.cit_toks


def populate_db_contexts(session, opinion_id: int, context_slice=slice(-128, 128)):
    unstructured_html = session.query(Opinion).filter(Opinion.resource_id == opinion_id).first().html_text
    if not unstructured_html:
        raise ValueError(f"No HTML for case {opinion_id}")
    unstructured_text = BeautifulSoup(unstructured_html, features="lxml").text
    clean_text = unstructured_text.replace("U. S.", "U.S.")
    tokenizer = OneTimeTokenizer()
    citations = list(eyecite.get_citations(clean_text, tokenizer=tokenizer))
    cited_resources = eyecite.resolve_citations(citations)
    reporter_resource_dict = {format_reporter(res.citation.groups.get('volume'), res.citation.groups.get('reporter'),
                                              res.citation.groups.get('page')): res
                              for res in cited_resources}
    stmt = select(Opinion).join(Cluster).where(Cluster.reporter.in_(reporter_resource_dict.keys()))
    opinions = []
    for opinion in session.execute(stmt).iterator:
        for citation in cited_resources[reporter_resource_dict[opinion.cluster.reporter]]:
            if isinstance(citation, CaseCitation):
                if citation.metadata.parenthetical is not None and not PARENTHETICAL_BLACKLIST_REGEX.match(
                        citation.metadata.parenthetical):
                    opinion.opinion_parentheticals.append(OpinionParenthetical(citing_opinion_id=opinion_id,
                                                                               cited_opinion_id=opinion.resource_id,
                                                                               text=citation.metadata.parenthetical))
                start = max(0, citation.index + context_slice.start)
                stop = min(len(tokenizer.words), citation.index + context_slice.stop)
                # contexts.append(list(self.clean_contexts(self.tokenizer.words[start:stop])))
                opinion.citation_contexts.append(CitationContext(citing_opinion_id=opinion_id,
                                                                 cited_opinion_id=opinion.resource_id,
                                                                 text="".join(
                                                                     [str(s) for s in tokenizer.words[start:stop]])))
        opinions.append(opinion)
    return opinions


def populate_all_db_contexts():
    from db.sqlalchemy.helpers import get_session
    s = get_session()
    for i, op in enumerate(s.execute(select(Opinion)).iterator):
        try:
            populate_db_contexts(s, op.resource_id)
        except Exception as e:
            Logger.error(f"Failed {op.resource_id} with {e}!")
            continue
        Logger.info(f"Completed {op.resource_id}")
        s.commit()


if __name__ == '__main__':
    populate_all_db_contexts()
