import logging

from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.http import (HttpResponseRedirect, HttpResponseForbidden)

from devmo.urlresolvers import reverse

from taggit.utils import parse_tags

import waffle

from access.decorators import login_required
from demos.models import Submission

from . import INTEREST_SUGGESTIONS
from .models import Calendar, Event, UserProfile, UserDocsActivityFeed
from .forms import UserProfileEditForm

from wiki.helpers import format_comment


DOCS_ACTIVITY_MAX_ITEMS = getattr(settings,
        'DOCS_ACTIVITY_MAX_ITEMS', 15)


def events(request):
    """Developer Engagement Calendar"""
    cal = Calendar.objects.get(shortname='devengage_events')
    events = Event.objects.filter(calendar=cal)
    upcoming_events = events.filter(done=False)
    past_events = events.filter(done=True)
    google_maps_api_key = getattr(settings, 'GOOGLE_MAPS_API_KEY',
        "ABQIAAAAijZqBZcz-rowoXZC1tt9iRT5rHVQFKUGOHoyfP"
        "_4KyrflbHKcRTt9kQJVST5oKMRj8vKTQS2b7oNjQ")

    return render(request, 'devmo/calendar.html', {
        'upcoming_events': upcoming_events,
        'past_events': past_events,
        'google_maps_api_key': google_maps_api_key
    })


def profile_view(request, username):
    profile = get_object_or_404(UserProfile, user__username=username)
    user = profile.user

    DEMOS_PAGE_SIZE = getattr(settings, 'DEMOS_PAGE_SIZE', 12)
    sort_order = request.GET.get('sort', 'created')
    try:
        page_number = int(request.GET.get('page', 1))
    except ValueError:
        page_number = 1
    show_hidden = (user == request.user) or user.is_superuser

    demos = Submission.objects.all_sorted(sort_order).filter(
                                                        creator=profile.user)
    if not show_hidden:
        demos = demos.exclude(hidden=True)

    demos_paginator = Paginator(demos, DEMOS_PAGE_SIZE, True)
    demos_page = demos_paginator.page(page_number)

    wiki_activity, docs_feed_items = None, None
    wiki_activity = profile.wiki_activity()

    return render(request, 'devmo/profile.html', dict(
        profile=profile, demos=demos, demos_paginator=demos_paginator,
        demos_page=demos_page, docs_feed_items=docs_feed_items,
        wiki_activity=wiki_activity
    ))


@login_required
def my_profile(request):
    user = request.user
    return HttpResponseRedirect(reverse(
            'devmo.views.profile_view', args=(user.username,)))


def profile_edit(request, username):
    """View and edit user profile"""
    profile = get_object_or_404(UserProfile, user__username=username)
    if not profile.allows_editing_by(request.user):
        return HttpResponseForbidden()

    # Map of form field names to tag namespaces
    field_to_tag_ns = (
        ('interests', 'profile:interest:'),
        ('expertise', 'profile:expertise:')
    )

    if request.method != 'POST':

        initial = dict(email=profile.user.email)

        # Load up initial websites with either user data or required base URL
        for name, meta in UserProfile.website_choices:
            initial['websites_%s' % name] = profile.websites.get(name, '')

        # Form fields to receive tags filtered by namespace.
        for field, ns in field_to_tag_ns:
            initial[field] = ', '.join(t.name.replace(ns, '')
                                       for t in profile.tags.all_ns(ns))

        # Finally, set up the form.
        form = UserProfileEditForm(instance=profile, initial=initial)

    else:
        form = UserProfileEditForm(request.POST, request.FILES,
                                   instance=profile)
        if form.is_valid():
            profile_new = form.save(commit=False)

            # Gather up all websites defined by the model, save them.
            sites = dict()
            for name, meta in UserProfile.website_choices:
                field_name = 'websites_%s' % name
                field_value = form.cleaned_data.get(field_name, '')
                if field_value and field_value != meta['prefix']:
                    sites[name] = field_value
            profile_new.websites = sites

            # Save the profile record now, since the rest of this deals with
            # related resources...
            profile_new.save()

            # Update tags from form fields
            for field, tag_ns in field_to_tag_ns:
                tags = [t.lower() for t in parse_tags(
                                            form.cleaned_data.get(field, ''))]
                profile_new.tags.set_ns(tag_ns, *tags)

            return HttpResponseRedirect(reverse(
                    'devmo.views.profile_view', args=(profile.user.username,)))

    return render(request, 'devmo/profile_edit.html', dict(
        profile=profile, form=form, INTEREST_SUGGESTIONS=INTEREST_SUGGESTIONS
    ))


@login_required
def my_profile_edit(request):
    user = request.user
    return HttpResponseRedirect(reverse(
            'devmo.views.profile_edit', args=(user.username,)))
