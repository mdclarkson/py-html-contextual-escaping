#!/usr/bin/env python -O

"""
Defines the rules for propagating context across snippets of static template
content.
"""

from autoesc.context import *
from autoesc import content, debug, escaping, html, js
from functools32 import lru_cache
from cStringIO import StringIO
import re

def context_union(context0, context1):
    """
    A context which is consistent with both contexts.  This should be
    used when multiple execution paths join, such as the path through
    the then-clause of an <code>{if}</code> command and the path
    through the else-clause.
    Returns STATE_ERROR when there is no such context consistent with both.
    """

    if context0 == context1:
        return context0

    if context0 == ((context1 & ~JS_CTX_ALL) | js_ctx_of(context0)):
        # The contexts differ only by JS_CTX_*
        return (context0 & ~JS_CTX_ALL) | JS_CTX_UNKNOWN

    url_part_0 = url_part_of(context0)
    if context0 == ((context1 & ~URL_PART_ALL) | url_part_0):
        # The contexts differ only by URL_PART
        return (context0 & ~URL_PART_ALL) | URL_PART_UNKNOWN

    # Allow a nudged context to join with an unnudged one.
    # This means that
    #   <p title={{if .C}}{{.}}{{end}}
    # ends in an unquoted value state even though the else branch
    # ends in stateBeforeValue.
    ncontext0 = force_epsilon_transition(context0)
    ncontext1 = force_epsilon_transition(context1)
    if context0 != ncontext0 or context1 != ncontext1:
        return context_union(ncontext0, ncontext1)

    return STATE_ERROR


# Characters that break a line in JavaScript source suitable for use in a
# regex charset.
NLS = u"\n\r\u2028\u2029"

def _end_of_attr_value(raw_text, delim):
    """
    Returns the end of the attribute value of -1 if delim indicates we are
    not in an attribute, or len(raw_text) if we are in an attribute but the
    end does not appear in raw_text.
    """
    if delim == DELIM_NONE:
        return -1
    if delim == DELIM_SPACE_OR_TAG_END:
        match = re.search(r'[\s>]', raw_text)
        if match:
            return match.start(0)
    else:
        quote = raw_text.find(DELIM_TEXT[delim])
        if quote >= 0:
            return quote
    return len(raw_text)


class _Transition(object):
    """
    Encapsulates a grammar production and the context after that
    production is seen in a chunk of HTML/CSS/JS input.
    """
    def __init__(self, pattern):
        if type(pattern) is type(re.compile('')):
            self.pattern = pattern
        else:
            self.pattern = re.compile(pattern)

    def is_applicable_to(self, prior, match):
        """
        True iff this transition can produce a context after the text in
        raw_text[0:match.start(0) + match.end(0)].
        This should not destructively modify the match.

        prior - The context prior to the token in match.
        match - The token matched by self.pattern.
        """
        assert self and type(prior) is int and hasattr(match, 'group')
        return True

    def compute_next_context(self, prior, match):
        """
        Computes the context that this production transitions to after
        raw_text[0:match.start(0) + match.end(0)].

        prior - The context prior to the token in match.
        match - The token matched by self.pattern.

        Returns the context after the given token.
        """
        raise NotImplementedError('abstract')  # pragma: no cover

    def raw_text(self, match):
        """
        Called to normalize the matched text.
        """
        assert self
        return match.string[:match.end()]


class _ToTransition(_Transition):
    """
    A transition to a given context.
    """

    def __init__(self, regex, dest):
        """dest - a context."""
        _Transition.__init__(self, regex)
        self.dest = dest

    def compute_next_context(self, prior, match):
        return self.dest


class _ToTagTransition(_Transition):
    """
    A transition to a context in the body of an open tag for the given
    element.
    """

    def __init__(self, regex, context):
        _Transition.__init__(self, regex)
        self.context = context

    def compute_next_context(self, prior, match):
        return self.context


class _NormalizeTransition(_Transition):
    """
    A transition that replaces the matched text with alternate text.
    """

    def __init__(self, transition, repl, replace_whole=False):
        _Transition.__init__(self, transition.pattern)
        self.transition = transition
        self.repl = repl
        self.replace_whole = replace_whole

    def compute_next_context(self, prior, match):
        return self.transition.compute_next_context(prior, match)

    def is_applicable_to(self, prior, match):
        return self.transition.is_applicable_to(prior, match)

    def raw_text(self, match):
        if self.replace_whole:
            return self.repl
        else:
            return ''.join((match.string[:match.start()], self.repl))


class _NormalizeJsBlockCommentTransition(_NormalizeTransition):
    """
    A normalizing transition for content in the body of a JS block comment.

    JS block comments are lexically significant since they are replaced with

    > 7.4 Comments
    > Comments behave like white space and are discarded except that, if a
    > MultiLineComment contains a line terminator character, then the entire
    > comment is considered to be a LineTerminator for purposes of parsing by
    > the syntactic grammar.

    which means that

    if (x) return /*
    */ f()

    is quite different from

    if (x) return /* */ f()

    This class normalizes any chunk of text that contains a line terminator
    character with '\n'.
    """

    def __init__(self, pattern):
        _NormalizeTransition.__init__(self, pattern, "", True)

    def raw_text(self, match):
        text = match.string[:match.end()]
        if re.search('[%s]' % NLS, text):
            return '\n'
        else:
            return ''

_TAG_DONE_ELEMENT_TO_PARTIAL_CONTEXT = {
    ELEMENT_NONE: STATE_TEXT,
    ELEMENT_SCRIPT: STATE_JS | JS_CTX_REGEX,
    ELEMENT_STYLE: STATE_CSS,
    ELEMENT_LISTING: STATE_RCDATA,
    ELEMENT_TEXTAREA: STATE_RCDATA,
    ELEMENT_TITLE: STATE_RCDATA,
    ELEMENT_XMP: STATE_RCDATA,
    }


class _TagDoneTransition(_Transition):
    """
    Transitions from the end of a tag to the content-type appropriate to its
    body.
    """

    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        el_type = element_type_of(prior)
        return _TAG_DONE_ELEMENT_TO_PARTIAL_CONTEXT[el_type] | el_type


class _TransitionBackToTag(_Transition):
    """
    A transition back to a context in the body of an open tag.
    """

    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        return STATE_TAG | element_type_of(prior)


class _TransitionToAttrName(_Transition):
    """
    A transition to a context in the name of an attribute whose attribute
    type is determined by its name seen thus far.
    """
    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        content_kind = html.attr_type(match.group(1))
        attr = attr_type_of(prior)
        if content_kind == content.CONTENT_KIND_JS:
            attr = ATTR_SCRIPT
        elif content_kind == content.CONTENT_KIND_CSS:
            attr = ATTR_STYLE
        elif content_kind == content.CONTENT_KIND_URL:
            attr = ATTR_URL
        return STATE_ATTR_NAME | element_type_of(prior) | attr


class _TransitionToAttrValue(_Transition):
    """
    A transition to a context in the name of an attribute of the given type.
    """
    def __init__(self, regex, delim):
        _Transition.__init__(self, regex)
        self.delim = delim

    def compute_next_context(self, prior, match):
        return after_attr_delimiter(
            element_type_of(prior), attr_type_of(prior), self.delim)


class _TransitionToState(_Transition):
    """
    Transitions to a particular state.
    """

    def __init__(self, regex, state):
        """A transition to the given state."""
        _Transition.__init__(self, regex)
        self.state = state

    def compute_next_context(self, prior, match):
        return (prior & ~(URL_PART_ALL | STATE_ALL)) | self.state


class _TransitionToJsString(_Transition):
    """
    Transitions to a JS string state.
    """

    def __init__(self, regex, state):
        """A transition to the given state."""
        _Transition.__init__(self, regex)
        self.state = state

    def compute_next_context(self, prior, match):
        return (
            (prior & (ELEMENT_ALL | ATTR_ALL | DELIM_ALL))
            | self.state)


class _SlashTransition(_Transition):
    """
    Transitions into a regular expression literal or not depending on the
    JS context bits.
    """

    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        if (js_ctx := js_ctx_of(prior)) == JS_CTX_DIV_OP:
            return ((prior & ~(STATE_ALL | JS_CTX_ALL))
                    | STATE_JS | JS_CTX_REGEX)
        elif js_ctx == JS_CTX_REGEX:
            return (prior & ~(STATE_ALL | JS_CTX_ALL)) | STATE_JSREGEXP
        else:
            raise ContextUpdateFailure(
                ("ambiguous / could start a division or a RegExp."
                 "  Please parenthesize near `%s`.")
                % match.string[match.start():])


class _JsPuncTransition(_Transition):
    """
    Keeps JS context bits up-to-date.
    """

    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        return js.next_js_ctx(match.group(0), prior)


class _TransitionToSelf(_Transition):
    """A transition that consumes some content without changing state."""
    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        return prior


# Consumes the entire content without change if nothing else matched.
_TRANSITION_TO_SELF = _TransitionToSelf(r'\Z')
# Matching at the end is lowest possible precedence.


class _URLPartTransition(_Transition):
    """
    Inside a URL attribute value, keeps track of which part of a hierarchical
    URL we are in.
    """

    def __init__(self, pattern):
        _Transition.__init__(self, pattern)

    def compute_next_context(self, prior, match):
        if (url_part := url_part_of(prior)) == URL_PART_NONE:
            if text := match.string[:match.end()].strip():
                # There is a non-space character preceding.
                url_part = URL_PART_PRE_QUERY

        if (url_part != URL_PART_QUERY_OR_FRAG
            # Matches '?', '#', or an encoded form thereof.
            and match.group(1)):
            url_part = URL_PART_QUERY_OR_FRAG
        return (prior & ~URL_PART_ALL) | url_part


_URL_PART_TRANSITION = _URLPartTransition(r'([?#])|\Z')
_CSSURL_PART_TRANSITION = _URLPartTransition(
    r'([?#]|\\(?:23|3[fF]|[?#]))|\Z')


class _EndTagTransition(_Transition):
    """
    Transition when we see the start of an end tag like '</foo'.
    """

    def __init__(self, pattern):
        '''Matches the end of a special tag like "script".'''
        _Transition.__init__(self, pattern)

    # TODO: This transitions to an HTML_TAG state which accepts attributes.
    # So we allow nonsensical constructs like </br foo="bar">.
    # Add another HTML_END_TAG state that just accepts space and >.
    def compute_next_context(self, prior, match):
        return STATE_TAG | ELEMENT_NONE

    def is_applicable_to(self, prior, match):
        return attr_type_of(prior) == ATTR_NONE


_SCRIPT_TAG_END = _EndTagTransition(r'(?i)<\/script\b')
_STYLE_TAG_END = _EndTagTransition(r'(?i)<\/style\b')

_ELEMENT_TO_TAG_NAME = {
    ELEMENT_TEXTAREA: "textarea",
    ELEMENT_TITLE: "title",
    ELEMENT_LISTING: "listing",
    ELEMENT_XMP: "xmp",
    }


class _RcdataEndTagTransition(_Transition):
    """
    Transition that handles exit from tags like <title> and <textarea>
    which cannot contain tags.
    """

    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        return STATE_TAG | ELEMENT_NONE

    def is_applicable_to(self, prior, match):
        return (
            match.group(1).lower()
            == _ELEMENT_TO_TAG_NAME.get(element_type_of(prior)))


class _CssUriTransition(_Transition):
    """
    Handles transition into CSS url(...) constructs.
    """

    def __init__(self, regex):
        """
        Matches the beginning of a CSS URL with the delimiter, if any,
        in group 1.
        """
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        delim = match.group(1)
        if "\"" == delim:
            state = STATE_CSSDQ_URL
        elif "'" == delim:
            state = STATE_CSSSQ_URL
        else:
            state = STATE_CSS_URL
        return (prior & ~(STATE_ALL | URL_PART_ALL)) | state | URL_PART_NONE


class _DivPreceder(_Transition):
    """
    Matches a portion of JavaScript that can precede a division operator.
    """

    def __init__(self, regex):
        _Transition.__init__(self, regex)

    def compute_next_context(self, prior, match):
        return ((prior & ~(STATE_ALL | JS_CTX_ALL))
                | STATE_JS | JS_CTX_DIV_OP)


# For each state, a group of token definitions and transitions to other states.
# The rules each have an associated pattern, and the rule whose pattern
# matches earliest in the text wins.
_TRANSITIONS = [None for _ in xrange(0, COUNT_OF_STATES)]
_TRANSITIONS[STATE_TEXT] = (
    _TransitionToSelf(r'\A[^<]+'),
    # Normalizing the '<!--' to '' elides comments from HTML text.
    _NormalizeTransition(_ToTransition(r'<!--', STATE_HTMLCMT), ""),
    _ToTagTransition(r'(?i)<script(?![a-z\-])', STATE_TAG | ELEMENT_SCRIPT),
    _ToTagTransition(r'(?i)<style(?![a-z\-])', STATE_TAG | ELEMENT_STYLE),
    _ToTagTransition(r'(?i)<textarea(?![a-z\-])', STATE_TAG | ELEMENT_TEXTAREA),
    _ToTagTransition(r'(?i)<title(?![a-z\-])', STATE_TAG | ELEMENT_TITLE),
    _ToTagTransition(r'(?i)<xmp(?![a-z\-])', STATE_TAG | ELEMENT_XMP),
    _NormalizeTransition(
        _TransitionToSelf(r'(?i)<(?!/?(?:[a-z]|\Z)|!doctype)'), "&lt;"),
    _ToTagTransition(r'</', STATE_HTML_BEFORE_TAG_NAME | ELEMENT_CLOSE),
    _ToTransition(r'<', STATE_HTML_BEFORE_TAG_NAME),
    )
_TRANSITIONS[STATE_RCDATA] = (
    _RcdataEndTagTransition(r'(?i)</([a-z\-]+)(?![a-z\-])'),
    _NormalizeTransition(_TransitionToSelf(r'<'), "&lt;"),
    _TRANSITION_TO_SELF,
    )
_TRANSITIONS[STATE_HTML_BEFORE_TAG_NAME] = (
    _ToTagTransition(r'\A[A-Za-z]+', STATE_TAG_NAME),
    _ToTransition(r'\A(?=[^A-Za-z])', STATE_TEXT),
    )
_TRANSITIONS[STATE_TAG_NAME] = (
    _TransitionToSelf(r'\A[A-Za-z0-9:-]*(?:[A-Za-z0-9]|\Z)'),
    _ToTagTransition(r'\A(?=[\/\s>])', STATE_TAG | ELEMENT_NONE),
    )
_TRANSITIONS[STATE_TAG] = (
    # Allows "data-foo" and other dashed attribute names, but
    # intentionally disallows "--" as an attribute name so that a tag ending
    # after a value-less attribute named "--" cannot be confused with a HTML
    # comment end ("-->").
    _TransitionToAttrName(r'\A\s*([A-Za-z][\w:-]*)'),
    _TagDoneTransition(r'\A\s*\/?>'),
    _TransitionToSelf(r'\A\s+\Z'),
    )
_TRANSITIONS[STATE_ATTR_NAME] = (
    _TransitionToSelf(r'[A-Za-z0-9\-]+'),
    # For a value-less attribute, make an epsilon transition back to the tag
    # body context to look for a tag end or another attribute name.
    _TransitionToState(r'\A', STATE_AFTER_NAME),
    )
_TRANSITIONS[STATE_AFTER_NAME] = (
    _TransitionToState(r'\A\s*=', STATE_BEFORE_VALUE),
    _TransitionToSelf(r'\A\s+'),
    _TransitionBackToTag(r'\A'),
    )
_TRANSITIONS[STATE_BEFORE_VALUE] = (
    _TransitionToAttrValue(r'\A\s*["]', DELIM_DOUBLE_QUOTE),
    _TransitionToAttrValue(r'\A\s*[\']', DELIM_SINGLE_QUOTE),
    _TransitionToAttrValue(r'\A(?=[^=\"\'\`\s>])',  # Unquoted value start.
                           DELIM_SPACE_OR_TAG_END),
    # Epsilon transition back if there is an empty value followed by a tag end:
    #    <input value=>
    _NormalizeTransition(_TransitionBackToTag(r'\A(?=/?>)'), '""'),
    _TransitionToSelf(r'\A\s+'),
    )
_TRANSITIONS[STATE_HTMLCMT] = (
    _NormalizeTransition(_ToTransition(r'-->', STATE_TEXT), "", True),
    _NormalizeTransition(_TRANSITION_TO_SELF, "", True),
    )
_TRANSITIONS[STATE_ATTR] = (
    _TRANSITION_TO_SELF,
    )
# The CSS transitions below are based on
# http://www.w3.org/TR/css3-syntax/#lexical
_TRANSITIONS[STATE_CSS] = (
    _NormalizeTransition(
        _TransitionToState(r'\/\*', STATE_CSSBLOCK_CMT),
        " "),
    # Non-standard but widely supported.
    _NormalizeTransition(
        _TransitionToState(r'\/\/', STATE_CSSLINE_CMT), ""),
    _TransitionToState(r'["]', STATE_CSSDQ_STR),
    _TransitionToState(r'[\']', STATE_CSSSQ_STR),
    _CssUriTransition(r'(?i)\burl\s*\(\s*([\"\']?)'),
    _STYLE_TAG_END,
    _TRANSITION_TO_SELF,
    )
_TRANSITIONS[STATE_CSSBLOCK_CMT] = (
    _NormalizeTransition(
        _TransitionToState(r'\*\/', STATE_CSS), "", True),
    _NormalizeTransition(
        _STYLE_TAG_END,
        "</style", True),
    _NormalizeTransition(_TRANSITION_TO_SELF, "", True),
    )
_TRANSITIONS[STATE_CSSLINE_CMT] = (
    _NormalizeTransition(
        _TransitionToState(r'[\n\f\r]', STATE_CSS),
        "\n", True),
    _NormalizeTransition(
        _STYLE_TAG_END,
        "</style", True),
    _NormalizeTransition(_TRANSITION_TO_SELF, "", True),
    )
_TRANSITIONS[STATE_CSSDQ_STR] = (
    _TransitionToState(r'["]', STATE_CSS),
    # Line continuation or escape.
    _TransitionToSelf(r'\\(?:\r\n?|[\n\f\"])'),
    _CSSURL_PART_TRANSITION,
    _ToTransition(r'[\n\r\f]', STATE_ERROR),
    _STYLE_TAG_END,  # TODO: Make this an error transition?
    _TRANSITION_TO_SELF,
    )
_TRANSITIONS[STATE_CSSSQ_STR] = (
    _TransitionToState(r'[\']', STATE_CSS),
    # Line continuation or escape.
    _TransitionToSelf(r'\\(?:\r\n?|[\n\f\'])'),
    _CSSURL_PART_TRANSITION,
    _ToTransition(r'[\n\r\f]', STATE_ERROR),
    _STYLE_TAG_END,  # TODO: Make this an error transition?
    )
_TRANSITIONS[STATE_CSS_URL] = (
    _TransitionToState(r'[\\)\s]', STATE_CSS),
    _CSSURL_PART_TRANSITION,
    _TransitionToState(r'[\"\']', STATE_ERROR),
    _STYLE_TAG_END,
    )
_TRANSITIONS[STATE_CSSSQ_URL] = (
    _TransitionToState(r'[\']', STATE_CSS),
    _CSSURL_PART_TRANSITION,
    # Line continuation or escape.
    _TransitionToSelf(r'\\(?:\r\n?|[\n\f\'])'),
    _ToTransition(r'[\n\r\f]', STATE_ERROR),
    _STYLE_TAG_END,
    )
_TRANSITIONS[STATE_CSSDQ_URL] = (
    _TransitionToState(r'["]', STATE_CSS),
    _CSSURL_PART_TRANSITION,
    # Line continuation or escape.
    _TransitionToSelf(r'\\(?:\r\n?|[\n\f\"])'),
    _ToTransition(r'[\n\r\f]', STATE_ERROR),
    _STYLE_TAG_END,
    )
_TRANSITIONS[STATE_JS] = (
    _NormalizeTransition(
        _TransitionToState(r'/[*]', STATE_JSBLOCK_CMT),
        # We need at least one space to prevent blurring of boundaries.
        #     1-/**/-1
        # should remain the token sequence
        #     1 - - 1
        # and not become the different token sequence
        #     1 -- 1
        # Similarly, the token sequence
        #     x</**//script|foo/i.match(s)[0]
        # should remain the token sequence
        #     x < /script|foo/i . match ( s ) [ 0 ]
        # and not become the invalid token sequence
        #     x </script ...
        " "),
    _NormalizeTransition(_TransitionToState(r'//', STATE_JSLINE_CMT), ""),
    _TransitionToJsString(r'["]', STATE_JSDQ_STR),
    _TransitionToJsString(r'[\']', STATE_JSSQ_STR),
    _SlashTransition(r'/'),
    # Shuffle words, punctuation (besides /), and numbers off to an
    # analyzer which uses is_regex_preceder to determine what '/' means.
    _JsPuncTransition(r'(?i)(?:[^<\/\"\'\s\\]|<(?!\/script))+'),
    _TransitionToSelf(r'\s+'),  # Space
    _SCRIPT_TAG_END,
    )
_TRANSITIONS[STATE_JSBLOCK_CMT] = (
    _NormalizeJsBlockCommentTransition(
        _TransitionToState(r'[*]/', STATE_JS)),
    _NormalizeTransition(_SCRIPT_TAG_END, "</script", True),
    _NormalizeJsBlockCommentTransition(_TRANSITION_TO_SELF),
    )
# Line continuations are not allowed in line comments.
_TRANSITIONS[STATE_JSLINE_CMT] = (
    _NormalizeTransition(_TransitionToState("[%s]" % NLS, STATE_JS),
                         "\n", True),
    _NormalizeTransition(_SCRIPT_TAG_END, "</script", True),
    _NormalizeTransition(_TRANSITION_TO_SELF, "", True),
    )
_TRANSITIONS[STATE_JSDQ_STR] = (
    _DivPreceder(r'["]'),
    _SCRIPT_TAG_END,
    _TransitionToSelf(
        r"(?i)" +                      # Case-insensitively
        r"\A(?:" +                     # from the start
            r"[^\"\\" + NLS + r"<]" +  # match all but nls, quotes, \s, <;
            r"|\\(?:" +                # or backslash followed by a
                r"\r\n?" +             # line continuation
                r"|[^\r<]" +           # or an escape
                r"|<(?!/script)" +     # or non-closing less-than.
            r")" +
            r"|<(?!/script)" +
        r")+"),
    )
_TRANSITIONS[STATE_JSSQ_STR] = (
    _DivPreceder(r'[\']'),
    _SCRIPT_TAG_END,
    _TransitionToSelf(
        r"(?i)" +
        r"\A(?:" +                     # Case-insensitively, from start
            r"[^\'\\" + NLS + "<]" +   # match all but nls, quotes, \s, <;
            r"|\\(?:" +                # or a backslash followed by a
                r"\r\n?" +             # line continuation
                r"|[^\r<]" +           # or an escape;
                r"|<(?!/script)" +     # or non-closing less-than.
            r")" +
            r"|<(?!/script)" +
        r")+"),
    )
_TRANSITIONS[STATE_JSREGEXP] = (
    _DivPreceder(r'/'),
    _SCRIPT_TAG_END,
    _TransitionToSelf(
        r"\A(?:" +
            # We have to handle [...] style character sets specially since
            # in /[/]/, the second solidus doesn't end the RegExp.
            r"[^\[\\/<" + NLS + "]" +        # A non-charset, non-escape token;
            r"|\\[^" + NLS + "]" +           # an escape;
            r"|\\?<(?!/script)" +
            r"|\[" +                         # or a character set containing
                r"(?:[^\]\\<" + NLS + "]" +  # normal characters,
                r"|\\(?:[^" + NLS + "]))*" + # and escapes;
                r"|\\?<(?!/script)" +        # or non-closing angle less-than.
            r"\]" +
        r")+"),
    )
    # TODO: Do we need to recognize URL attributes that start with
    # javascript:, data:text/html, etc. and transition to JS instead
    # with a second layer of percent decoding triggered by a protocol
    # in (DATA, JAVASCRIPT, NONE) added to Context?
_TRANSITIONS[STATE_URL] = (_URL_PART_TRANSITION,)

_TRANSITIONS = tuple(_TRANSITIONS)


def _process_next_token(text, context):
    """
    Consume a portion of text and compute the next context.
    Output is stored in member variables.
    text - Non empty.

    Returns (n, context after text[:n], replacement for text[:n])
    """

    if is_error_context(context):  # The ERROR state is infectious.
        return (len(text), context, text)

    # Find the transition whose pattern matches earliest
    # in the raw text.
    earliest_start = len(text)+1
    earliest_transition = None
    earliest_match = None

    for transition in _TRANSITIONS[state_of(context)]:
        match = transition.pattern.search(text)
        if not match:
            continue
        start = match.start(0)
        if (start < earliest_start
            and transition.is_applicable_to(context, match)):
            earliest_start = start
            earliest_transition = transition
            earliest_match = match

    if earliest_transition:
        num_consumed = earliest_match.end(0)
        next_context = earliest_transition.compute_next_context(
            context, earliest_match)
        normalized_text = earliest_transition.raw_text(earliest_match)
    else:
        num_consumed = len(text)
        next_context = STATE_ERROR
        normalized_text = text

    if (not num_consumed
        and state_of(next_context) == state_of(context)):  # pragma: no cover
        # Infinite loop.
        raise Exception('inf loop. for %r in %s'
                        % (text, debug.context_to_string(context)))

    return (num_consumed, next_context, normalized_text)


[lru_cache]
def process_raw_text(raw_text, context):
    """
    raw_text - A chunk of HTML/CSS/JS.
    context - The context before raw_text.

    Returns (
      the context after raw_text which may be an error context,
      a normalized version of the text or None if an error occurred,
      None or the context immediately prior to the error,
      None or the unprocessed suffix of raw_text when the error occurred)

    May raise ContextUpdateFailure which is equivalent to returning
    STATE_ERROR but with a more informative error message.
    """

    normalized = StringIO()

    while raw_text:
        prior_context, prior_raw_text = context, raw_text

        delim_type = delim_type_of(context)

        # If we are in an attribute value, then decode raw_text (except
        # for the delimiter) up to the next occurrence of delimiter.

        # The end of the section to decode.  Either before a delimiter
        # or > symbol that closes an attribute, at the end of the raw_text,
        # or -1 if no decoding needs to happen.

        if (attr_value_end := _end_of_attr_value(raw_text, delim_type)) == -1:
            # Outside an attribute value.  No need to decode.
            num_consumed, context, replacement_text = _process_next_token(
                raw_text, context)
            raw_text = raw_text[num_consumed:]
            normalized.write(replacement_text)

            if delim_type_of(context) == DELIM_SPACE_OR_TAG_END:
                # Introduce a double quote when we transition into an unquoted
                # attribute body.
                normalized.write('"')
        else:
            # Inside an attribute value.  Find the end and decode up to it.

            if delim_type == DELIM_SPACE_OR_TAG_END:
                # Check for suspicious characters in the value.
                # http://www.w3.org/TR/html5/tokenization.html
                # #attribute-value-unquoted-state
                # identifies [\0"'<=`] as transitions to error states.
                # If they occur in an unquoted value they are almost surely
                # an indication of an error in the template.
                bad = re.search(r'[\x00"\'<=`]', raw_text[:attr_value_end])
                if bad:
                    raise ContextUpdateFailure(
                        '%r in unquoted attr: %r'
                        % (bad.group(), raw_text[:attr_value_end]))

            # All of the languages we deal with (HTML, CSS, and JS) use
            # quotes as delimiters.
            # When one language is embedded in the other, we need to
            # decode delimiters before trying to parse the content in the
            # embedded language.

            # For example, in
            #       <a onclick="alert(&quot;Hello {$world}&quot;)">
            # the decoded value of the event handler is
            #       alert("Hello {$world}")
            # so to determine the appropriate escaping convention we decode
            # the attribute value before delegating to _process_next_token.

            # We could take the cross-product of two languages to avoid
            # decoding but that leads to either an explosion in the
            # number of states, or the amount of lookahead required.

            # The end of the attribute value.  At attr_value_end, or
            # attr_value_end + 1 if a delimiter needs to be consumed.
            if attr_value_end < len(raw_text):
                attr_end = attr_value_end + len(DELIM_TEXT[delim_type])
            else:
                attr_end = -1

            # Decode so that the JavaScript rules work on attribute values
            # like
            #     <a onclick='alert(&quot;{$msg}!&quot;)'>

            # If we've already processed the tokens "<a", " onclick='" to
            # get into the single quoted JS attribute context, then we do
            # three things:
            #   (1) This class will decode "&quot;" to "\"" and work below
            #       to go from STATE_JS to STATE_JSDQ_STR.
            #   (2) Then the caller checks {$msg} and realizes that $msg is
            #       part of a JS string.
            #   (3) Then, the above will identify the "'" as the end, and
            #       so we reach here with:
            #       r a w T e x t = " ! & q u o t ; ) ' > "
            #                                         ^ ^
            #                            attr_value_end attr_end

            # We use this example more in the comments below.

            attr_value_tail = html.unescape_html(raw_text[:attr_value_end])
            # attr_value_tail is "!\")" in the example above.

            if delim_type == DELIM_SINGLE_QUOTE:
                escaper = escaping.escape_html_sq_only
            else:
                escaper = escaping.escape_html_dq_only

            # Recurse on the decoded value.
            while attr_value_tail:
                num_consumed, context, replacement = _process_next_token(
                    attr_value_tail, context)
                attr_value_tail = attr_value_tail[num_consumed:]
                normalized.write(escaper(replacement))

            # TODO: Maybe check that context is legal to end an attr in.
            # Throw if the attribute ends inside a quoted string.

            if attr_end != -1:
                raw_text = raw_text[attr_end:]
                # raw_text is now ">" from the example above.

                # When an attribute ends, we're back in the tag.
                context = STATE_TAG | element_type_of(context)

                # Append the delimiter on exiting an attribute.
                if delim_type == DELIM_SINGLE_QUOTE:
                    normalized.write("'")
                else:
                    # Inserts an end quote for unquoted attributes.
                    normalized.write('"')
            else:
                # Whole tail is part of an unterminated attribute.
                if attr_value_end != len(raw_text):  # pragma: no cover
                    raise AssertionError()  # Illegal state.
                raw_text = ""
        if is_error_context(context):
            return (context, None, prior_context, prior_raw_text)
    return (context, normalized.getvalue(), None, None)


class ContextUpdateFailure(BaseException):
    """
    Raised on failure to update context to carry an informative error message
    when STATE_ERROR doesn't cut it.
    """
    def __init__(self, msg):
        BaseException.__init__(self, msg)


# TODO: If we need to deal with untrusted templates, then we need to make
# sure that tokens like <!--, </script>, etc. are never split with empty
# strings.
# We could do this by walking all possible paths through each template
# (both branches for ifs, each case for switches, and the 0,1, and 2+
# iteration case for loops).
# For each template, tokenize the original's raw_text nodes using
# RawTextContextUpdater and then tokenize one single raw_text node made by
# concatenating all raw_text.
# If one contains a sensitive token, e.g. <!--/ and the other doesn't, then
# we have a potential splitting attack.
# That and disallow unquoted attributes, and be paranoid about prints
# especially in the TAG_NAME productions.
