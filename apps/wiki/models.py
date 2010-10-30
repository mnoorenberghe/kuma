from collections import namedtuple
from datetime import datetime
from itertools import chain

from pyquery import PyQuery
from tower import ugettext_lazy as _lazy, ugettext as _

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models

from sumo import ProgrammingError
from sumo_locales import LOCALES
from sumo.models import ModelBase, TaggableMixin
from sumo.urlresolvers import reverse
from wiki import TEMPLATE_TITLE_PREFIX


# Disruptiveness of edits to translated versions. Numerical magnitude indicate
# the relative severity.
TYPO_SIGNIFICANCE = 10
MEDIUM_SIGNIFICANCE = 20
MAJOR_SIGNIFICANCE = 30
SIGNIFICANCES = (
    (TYPO_SIGNIFICANCE,
     _lazy('Minor details like punctuation and spelling errors')),
    (MEDIUM_SIGNIFICANCE,
     _lazy("Content changes that don't require immediate translation")),
    (MAJOR_SIGNIFICANCE,
     _lazy('Major content changes that will make older translations '
           'inaccurate')),
)

CATEGORIES = (
    (1, _lazy('Troubleshooting')),
    (2, _lazy('How to contribute')),
    (3, _lazy('Administration')),
    (4, _lazy('Templates')),
)

# FF versions used to filter article searches, power {for} tags, etc.:
#
# Iterables of (ID, name, abbreviation for {for} tags, max version this version
# group encompasses) grouped into optgroups. To add the ability to sniff a new
# version of an existing browser (assuming it doesn't change the user agent
# string too radically), you should need only to add a line here; no JS
# required. Just be wary of inexact floating point comparisons when setting
# max_version, which should be read as "From the next smaller max_version up to
# but not including version x.y".
VersionMetadata = namedtuple('VersionMetadata', 'id, name, slug, max_version')
GROUPED_FIREFOX_VERSIONS = (
    (_lazy('Desktop:'), (
        # The first option is the default for {for} display. This should be the
        # newest version.
        VersionMetadata(1, _lazy('Firefox 4.0'), 'fx4', 4.9999),
        VersionMetadata(2, _lazy('Firefox 3.5-3.6'), 'fx35', 3.9999),
        VersionMetadata(3, _lazy('Firefox 3.0'), 'fx3', 3.4999))),
    (_lazy('Mobile:'), (
        VersionMetadata(4, _lazy('Firefox Mobile 1.1'), 'm11', 1.9999),
        VersionMetadata(5, _lazy('Firefox Mobile 1.0'), 'm1', 1.0999))))

# Flattened:  # TODO: perhaps use optgroups everywhere instead
FIREFOX_VERSIONS = tuple(chain(*[options for label, options in
                                 GROUPED_FIREFOX_VERSIONS]))

# OSes used to filter articles and declare {for} sections:
OsMetaData = namedtuple('OsMetaData', 'id, name, slug')
GROUPED_OPERATING_SYSTEMS = (
    (_lazy('Desktop OS:'), (
        OsMetaData(1, _lazy('Windows'), 'win'),
        OsMetaData(2, _lazy('Mac OS X'), 'mac'),
        OsMetaData(3, _lazy('Linux'), 'linux'))),
    (_lazy('Mobile OS:'), (
        OsMetaData(4, _lazy('Maemo'), 'maemo'),
        OsMetaData(5, _lazy('Android'), 'android'))))

# Flattened
OPERATING_SYSTEMS = tuple(chain(*[options for label, options in
                                  GROUPED_OPERATING_SYSTEMS]))


REDIRECT_HTML = '<p>REDIRECT <a '  # how a redirect looks as rendered HTML
REDIRECT_CONTENT = 'REDIRECT [[%s]]'
REDIRECT_TITLE = _lazy('%(old)s Redirect %(number)i')
REDIRECT_SLUG = _lazy('%(old)s-redirect-%(number)i')


class TitleCollision(Exception):
    """An attempt to create two pages of the same title in one locale"""


class SlugCollision(Exception):
    """An attempt to create two pages of the same slug in one locale"""


def _inherited(parent_attr, direct_attr):
    """Return a descriptor delegating to an attr of the original document.

    If `self` is a translation, the descriptor delegates to the attribute
    `parent_attr` from the original document. Otherwise, it delegates to the
    attribute `direct_attr` from `self`.

    """
    getter = lambda self: (getattr(self.parent, parent_attr)
                               if self.parent
                           else getattr(self, direct_attr))
    setter = lambda self, val: (setattr(self.parent, parent_attr,
                                        val) if self.parent else
                                setattr(self, direct_attr, val))
    return property(getter, setter)


class Document(ModelBase, TaggableMixin):
    """A localized knowledgebase document, not revision-specific."""
    title = models.CharField(max_length=255, db_index=True)
    slug = models.CharField(max_length=255, db_index=True)

    # Is this document a template or not?
    # TODO: Localizing templates does not allow changing titles
    is_template = models.BooleanField(default=False, editable=False,
                                      db_index=True)
    # Is this document localizable or not?
    is_localizable = models.BooleanField(default=True, db_index=True)

    # TODO: validate (against settings.SUMO_LANGUAGES?)
    locale = models.CharField(max_length=7, db_index=True,
                              default=settings.WIKI_DEFAULT_LANGUAGE,
                              choices=settings.LANGUAGE_CHOICES)

    # Latest approved revision. L10n dashboard depends on this being so (rather
    # than being able to set it to earlier approved revisions). (Remove "+" to
    # enable reverse link.)
    current_revision = models.ForeignKey('Revision', null=True,
                                         related_name='current_for+')

    # The Document I was translated from. NULL iff this doc is in the default
    # locale or it is nonlocalizable. TODO: validate against
    # settings.WIKI_DEFAULT_LANGUAGE.
    parent = models.ForeignKey('self', related_name='translations', null=True)

    # Related documents, based on tags in common.
    # The RelatedDocument table is populated by
    # wiki.cron.calculate_related_documents.
    related_documents = models.ManyToManyField('self',
                                               through='RelatedDocument',
                                               symmetrical=False)

    # Cached HTML rendering of approved revision's wiki markup:
    html = models.TextField(editable=False)

    # Uncomment if/when we need a denormalized flag for how significantly
    # outdated this translation is. We probably will to support the dashboard.
    # If you do this, also make a periodic task to audit it occasionally.
    #
    # outdated = IntegerField(choices=SIGNIFICANCES, editable=False)

    category = models.IntegerField(choices=CATEGORIES, db_index=True)
    # firefox_versions,
    # operating_systems:
    #    defined in the respective classes below. Use them as in
    #    test_firefox_versions.

    # TODO: Rethink indexes once controller code is near complete. Depending on
    # how MySQL uses indexes, we probably don't need individual indexes on
    # title and locale as well as a combined (title, locale) one.
    class Meta(object):
        unique_together = (('parent', 'locale'), ('title', 'locale'),
                           ('slug', 'locale'))

    def _collides(self, attr, value):
        """Return whether there exists a doc in this locale whose `attr` attr
        is equal to mine."""
        return Document.uncached.filter(locale=self.locale,
                                        **{attr: value}).exists()

    def _raise_if_collides(self, attr, exception):
        """Raise an exception if a page of this title/slug already exists."""
        if self.id is None or hasattr(self, 'old_' + attr):
            # If I am new or my title/slug changed...
            if self._collides(attr, getattr(self, attr)):
                raise exception

    def clean(self):
        """Translations can't be localizable."""
        if self.locale != settings.WIKI_DEFAULT_LANGUAGE:
            self.is_localizable = False

    def _validate_is_localizable(self):
        """is_localizable == allowed to have translations. Make sure that isn't
        violated.

        For default language (en-US), is_localizable means it can have
        translations. Enforce:
            * is_localizable=True if it has translations
            * if has translations, unable to make is_localizable=False

        """
        # Can't translate if parent not localizable
        if self.parent and not self.parent.is_localizable:
            raise ValidationError('"%s": parent "%s" is not localizable.' % (
                                  unicode(self), unicode(self.parent)))

        # Can't make not localizable if it has translations
        # This only applies to documents that already exist, hence self.pk
        # TODO: Use uncached manager here, if we notice problems
        if self.pk and not self.is_localizable and self.translations.exists():
            raise ValidationError('"%s": parent "%s" has translations but is '
                                  'not localizable.' % (
                                  unicode(self), unicode(self.parent)))

    def _attr_for_redirect(self, attr, template):
        """Return the slug or title for a new redirect.

        `template` is a Python string template with "old" and "number" tokens
        used to create the variant.

        """
        def unique_attr():
            """Return a variant of getattr(self, attr) such that there is no
            Document of my locale with string attribute `attr` equal to it.

            Never returns the original attr value.

            """
            # "My God, it's full of race conditions!"
            i = 1
            while True:
                new_value = template % dict(old=getattr(self, attr), number=i)
                if not self._collides(attr, new_value):
                    return new_value
                i += 1

        old_attr = 'old_' + attr
        if hasattr(self, old_attr):
            # My slug (or title) is changing; we can reuse it for the redirect.
            return getattr(self, old_attr)
        else:
            # Come up with a unique slug (or title):
            return unique_attr()

    def save(self, *args, **kwargs):
        self.is_template = self.title.startswith(TEMPLATE_TITLE_PREFIX)

        self._raise_if_collides('slug', SlugCollision)
        self._raise_if_collides('title', TitleCollision)

        # This is too important to leave to a (possibly omitted) is_valid call.
        self._validate_is_localizable()

        super(Document, self).save(*args, **kwargs)

        # Make redirects if there's an approved revision and title or slug
        # changed. Allowing redirects for unapproved docs would (1) be of
        # limited use and (2) require making Revision.creator nullable.
        slug_changed = hasattr(self, 'old_slug')
        title_changed = hasattr(self, 'old_title')
        if self.current_revision and (slug_changed or title_changed):
            # TODO: Mark the redirect doc as unlocalizable when there is way to
            # represent that.
            doc = Document.objects.create(locale=self.locale,
                                          title=self._attr_for_redirect(
                                              'title', REDIRECT_TITLE),
                                          slug=self._attr_for_redirect(
                                              'slug', REDIRECT_SLUG),
                                          category=self.category)
            Revision.objects.create(document=doc,
                                    content=REDIRECT_CONTENT % self.title,
                                    is_approved=True,
                                    reviewer=self.current_revision.creator,
                                    creator=self.current_revision.creator)

            if slug_changed:
                del self.old_slug
            if title_changed:
                del self.old_title

    def __setattr__(self, name, value):
        """Trap setting slug and title, recording initial value."""
        # Public API: delete the old_title or old_slug attrs after changing
        # title or slug (respectively) to suppress redirect generation.
        if getattr(self, 'id', None):
            # I have been saved and so am worthy of a redirect.
            if name in ('slug', 'title') and hasattr(self, name):
                old_name = 'old_' + name
                if not hasattr(self, old_name):
                    if getattr(self, name) != value:
                        # Save original value:
                        setattr(self, old_name, getattr(self, name))
                elif value == getattr(self, old_name):
                    # They changed the attr back to its original value.
                    delattr(self, old_name)
        super(Document, self).__setattr__(name, value)

    @property
    def content_parsed(self):
        return self.html

    @property
    def language(self):
        return settings.LANGUAGES[self.locale.lower()]

    # FF version and OS are hung off the original, untranslated document and
    # dynamically inherited by translations:
    firefox_versions = _inherited('firefox_versions', 'firefox_version_set')
    operating_systems = _inherited('operating_systems', 'operating_system_set')

    def get_absolute_url(self):
        return reverse('wiki.document', locale=self.locale, args=[self.slug])

    def redirect_url(self):
        """If I am a redirect, return the absolute URL to which I redirect.

        Otherwise, return None.

        """
        # If a document starts with REDIRECT_HTML and contains any <a> tags
        # with hrefs, return the href of the first one. This trick saves us
        # from having to parse the HTML every time.
        if self.html.startswith(REDIRECT_HTML):
            anchors = PyQuery(self.html)('a[href]')
            if anchors:
                return anchors[0].get('href')
        return None

    def __unicode__(self):
        return '[%s] %s' % (self.locale, self.title)

    def allows_revision_by(self, user):
        """Return whether `user` is allowed to create new revisions of me.

        The motivation behind this method is that templates and other types of
        docs may have different permissions.

        """
        # TODO: Add tests for templateness or whatever is required.
        return user.has_perm('wiki.add_revision')

    def allows_editing_by(self, user):
        """Return whether `user` is allowed to edit document-level metadata.

        If the Document doesn't have a current_revision (nothing approved) then
        all the Document fields are still editable. Once there is an approved
        Revision, the Document fields can only be edited by privileged users.

        """
        return (not self.current_revision or
                user.has_perm('wiki.change_document'))

    def translated_to(self, locale):
        """Return the translation of me to the given locale.

        If there is no such Document, return None.

        """
        if self.locale != settings.WIKI_DEFAULT_LANGUAGE:
            raise NotImplementedError('translated_to() is implemented only on'
                                      'Documents in the default language so'
                                      'far.')
        try:
            return Document.objects.get(locale=locale, parent=self)
        except Document.DoesNotExist:
            return None

    @property
    def original(self):
        """Return the document I was translated from or, if none, myself."""
        return self.parent or self

    def has_voted(self, request):
        """Did the user already vote for this document?"""
        if request.user.is_authenticated():
            qs = HelpfulVote.objects.filter(document=self,
                                            creator=request.user)
        elif request.anonymous.has_id:
            anon_id = request.anonymous.anonymous_id
            qs = HelpfulVote.objects.filter(document=self,
                                            anonymous_id=anon_id)
        else:
            return False

        return qs.exists()


class Revision(ModelBase):
    """A revision of a localized knowledgebase document"""
    document = models.ForeignKey(Document, related_name='revisions')
    summary = models.TextField()  # wiki markup
    content = models.TextField()  # wiki markup

    # Keywords are used mostly to affect search rankings. Moderators may not
    # have the language expertise to translate keywords, so we put them in the
    # Revision so the translators can handle them:
    keywords = models.CharField(max_length=255, blank=True)

    created = models.DateTimeField(default=datetime.now)
    reviewed = models.DateTimeField(null=True)
    significance = models.IntegerField(choices=SIGNIFICANCES, null=True)
    comment = models.CharField(max_length=255)
    reviewer = models.ForeignKey(User, related_name='reviewed_revisions',
                                 null=True)
    creator = models.ForeignKey(User, related_name='created_revisions')
    is_approved = models.BooleanField(default=False, db_index=True)

    # The default locale's rev that was current when the Edit button was hit to
    # create this revision. Used to determine whether localizations are out of
    # date.
    based_on = models.ForeignKey('self', null=True, blank=True)
    # TODO: limit_choices_to={'document__locale':
    # settings.WIKI_DEFAULT_LANGUAGE} is a start but not sufficient.

    def _based_on_is_clean(self):
        """Return a tuple: (the correct value of based_on, whether the old
        value was correct).

        based_on must be a revision of the English version of the document if
        there are any such revisions and None otherwise. If based_on is not
        already set when this is called, the return value defaults to the
        current_revision of the English document.

        """
        if self.document.original.current_revision:
            if (self.based_on and
                self.based_on.document != self.document.original):
                # based_on is set and points to the wrong doc.
                return self.document.original.current_revision, False
            # else based_on is valid; leave it alone
        elif self.based_on:
            return None, False
        return self.based_on, True

    def clean(self):
        """Ensure based_on is valid."""
        # All of the cleaning herein should be unnecessary unless the user
        # messes with hidden form data.
        try:
            self.document and self.document.original
        except Document.DoesNotExist:
            # For clean()ing forms that don't have a document instance behind
            # them yet
            self.based_on = None
        else:
            based_on, is_clean = self._based_on_is_clean()
            if not is_clean:
                old = self.based_on
                self.based_on = based_on  # Be nice and guess a correct value.
                raise ValidationError(_('A revision must be based on an '
                    'approved revision of the %(locale)s document. Revision ID'
                    ' %(id)s does not fit those criteria.') %
                    dict(locale=LOCALES[settings.WIKI_DEFAULT_LANGUAGE].native,
                         id=old.id))

    def save(self, *args, **kwargs):
        _, is_clean = self._based_on_is_clean()
        if not is_clean:  # No more Mister Nice Guy
            raise ProgrammingError('Revision.based_on must be None or refer to'
                                   ' an approved revision of the default-'
                                   'language document.')

        super(Revision, self).save(*args, **kwargs)

        # When a revision is approved, re-cache the document's html content
        if self.is_approved and (
                not self.document.current_revision or
                self.document.current_revision.id < self.id):
            from wiki.parser import wiki_to_html
            locale = self.document.locale
            self.document.html = wiki_to_html(self.content, locale)
            self.document.current_revision = self
            self.document.save()

    def __unicode__(self):
        return u'[%s] %s #%s: %s' % (self.document.locale,
                                      self.document.title,
                                      self.id, self.content[:50])

    @property
    def content_parsed(self):
        from wiki.parser import wiki_to_html
        return wiki_to_html(self.content, self.document.locale)


# FirefoxVersion and OperatingSystem map many ints to one Document. The
# enumeration table of int-to-string is not represented in the DB because of
# difficulty working DB-dwelling gettext keys into our l10n workflow.
class FirefoxVersion(ModelBase):
    """A Firefox version, version range, etc. used to categorize documents"""
    item_id = models.IntegerField(choices=[(v.id, v.name) for v in
                                           FIREFOX_VERSIONS])
    document = models.ForeignKey(Document, related_name='firefox_version_set')

    class Meta(object):
        unique_together = ('item_id', 'document')


class OperatingSystem(ModelBase):
    """An operating system used to categorize documents"""
    item_id = models.IntegerField(choices=[(o.id, o.name) for o in
                                           OPERATING_SYSTEMS])
    document = models.ForeignKey(Document, related_name='operating_system_set')

    class Meta(object):
        unique_together = ('item_id', 'document')


class HelpfulVote(ModelBase):
    """Helpful or Not Helpful vote on Document."""
    document = models.ForeignKey(Document, related_name='poll_votes')
    helpful = models.BooleanField(default=False)
    created = models.DateTimeField(default=datetime.now, db_index=True)
    creator = models.ForeignKey(User, related_name='poll_votes', null=True)
    anonymous_id = models.CharField(max_length=40, db_index=True)
    user_agent = models.CharField(max_length=1000)


class RelatedDocument(ModelBase):
    document = models.ForeignKey(Document, related_name='related_from')
    related = models.ForeignKey(Document, related_name='related_to')
    in_common = models.IntegerField()

    class Meta(object):
        ordering = ['-in_common']
