# Python
import re

# Django
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib import messages
from django.core.urlresolvers import reverse_lazy
from django.db.models import Q
from django.shortcuts import redirect
from django.utils.translation import ugettext as _
from django.utils.translation import ungettext
from django.views.generic import DetailView, UpdateView

# Third party
from braces.views import (
    JSONResponseMixin,
    LoginRequiredMixin,
    SuperuserRequiredMixin,
)

# AstroBin
from astrobin.forms import (
    ImageFlagThumbsForm,
    ImageRevisionUploadForm,
    PrivateMessageForm,
)
from astrobin.models import (
    Image, ImageRevision,
    DeepSky_Acquisition,
    SolarSystem_Acquisition,
    LANGUAGES,
    LICENSE_CHOICES,
    SOLAR_SYSTEM_SUBJECT_CHOICES,
)
from astrobin.utils import to_user_timezone

# AstroBin apps
from nested_comments.models import NestedComment
from rawdata.forms import (
    PublicDataPool_SelectExistingForm,
    PrivateSharedFolder_SelectExistingForm,
)
from rawdata.models import PrivateSharedFolder


class ImageFlagThumbsView(
        LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    form_class = ImageFlagThumbsForm
    model = Image
    pk_url_kwarg = 'id'
    http_method_names = ('post',)

    def get_queryset(self):
        return Image.all_objects.filter(user = self.request.user)

    def get_success_url(self):
        image = self.get_object()
        return reverse_lazy('image_detail', args = (image.pk,))

    def post(self, request, *args, **kwargs):
        image = self.get_object()
        image.thumbnail_invalidate(False)
        for r in image.revisions.all():
            r.thumbnail_invalidate(False)
        messages.success(self.request, _("Thanks for reporting the problem. All thumbnails will be generated again."))
        return super(ImageFlagThumbsView, self).post(self.request, args, kwargs)


class ImageThumbView(JSONResponseMixin, DetailView):
    model = Image
    pk_url_kwarg = 'id'

    def get(self, request, *args, **kwargs):
        image = self.get_object()
        url = image.thumbnail(kwargs.pop('alias'), {
            'revision_label': kwargs.pop('r'),
            'animated': self.request.GET.get('animated', False),
        })

        return self.render_json_response({
            'id': image.pk,
            'url': url
        })


class ImageRawThumbView(DetailView):
    model = Image
    pk_url_kwarg = 'id'

    def get(self, request, *args, **kwargs):
        image = self.get_object()
        alias = kwargs.pop('alias')
        r = kwargs.pop('r')
        url = image.thumbnail(alias, {'revision_label': r})
        return redirect(url)


class ImageDetailView(DetailView):
    model = Image
    pk_url_kwarg = 'id'
    template_name = 'image/detail.html'
    template_name_suffix = ''

    def get_queryset(self):
        return Image.all_objects.all()

    def dispatch(self, request, *args, **kwargs):
        # Redirect to the correct revision
        image = Image.all_objects.get(pk = kwargs['id'])
        revision_label = kwargs['r']

        if revision_label is None:
            # No revision specified, let's see if we need to redirect to the
            # final.
            if image.is_final == False:
                final = image.revisions.filter(is_final = True)
                if final.count() > 0:
                    return redirect(reverse_lazy(
                        'image_detail',
                        args = (image.pk, final[0].label,)))

        return super(ImageDetailView, self).dispatch(request, *args, **kwargs)

    # TODO: this function is too long
    def get_context_data(self, **kwargs):
        context = super(ImageDetailView, self).get_context_data(**kwargs)

        image = context['object']
        r = self.request.GET.get('r')

        revision_image = None
        instance_to_platesolve = image
        is_revision = False
        if r != 0:
            try:
                revision_image = ImageRevision.objects.filter(image = image, label = r)[0]
                instance_to_platesolve = revision_image
                is_revision = True
            except:
                pass

        #############################
        # GENERATE ACQUISITION DATA #
        #############################
        from astrobin.moon import MoonPhase;

        gear_list = (
            (ungettext('Imaging telescope or lens',
                       'Imaging telescopes or lenses',
                       len(image.imaging_telescopes.all())),
             image.imaging_telescopes.all(), 'imaging_telescopes'),
            (ungettext('Imaging camera',
                       'Imaging cameras',
                       len(image.imaging_cameras.all())),
             image.imaging_cameras.all(), 'imaging_cameras'),
            (ungettext('Mount',
                       'Mounts',
                       len(image.mounts.all())),
             image.mounts.all(), 'mounts'),
            (ungettext('Guiding telescope or lens',
                       'Guiding telescopes or lenses',
                       len(image.guiding_telescopes.all())),
             image.guiding_telescopes.all(), 'guiding_telescopes'),
            (ungettext('Guiding camera',
                       'Guiding cameras',
                       len(image.guiding_cameras.all())),
             image.guiding_cameras.all(), 'guiding_cameras'),
            (ungettext('Focal reducer',
                       'Focal reducers',
                       len(image.focal_reducers.all())),
             image.focal_reducers.all(), 'focal_reducers'),
            (_('Software'), image.software.all(), 'software'),
            (ungettext('Filter',
                       'Filters',
                       len(image.filters.all())),
             image.filters.all(), 'filters'),
            (ungettext('Accessory',
                       'Accessories',
                       len(image.accessories.all())),
             image.accessories.all(), 'accessories'),
        )

        gear_list_has_commercial = False
        gear_list_has_paid_commercial = False
        for g in gear_list:
            if g[1].exclude(commercial = None).count() > 0:
                gear_list_has_commercial = True
                break
        for g in gear_list:
            for i in g[1].exclude(commercial = None):
                if i.commercial.is_paid() or i.commercial.producer == self.request.user:
                    gear_list_has_paid_commercial = True
                    # It would be faster if we exited the outer loop, but really,
                    # how many gear items can an image have?
                    break

        makes_list = ','.join(
            filter(None, reduce(
                lambda x,y: x+y,
                [list(x.values_list('make', flat = True)) for x in [y[1] for y in gear_list]])))

        deep_sky_acquisitions = DeepSky_Acquisition.objects.filter(image=image)
        ssa = None
        image_type = None
        deep_sky_data = {}

        try:
            ssa = SolarSystem_Acquisition.objects.get(image=image)
        except SolarSystem_Acquisition.DoesNotExist:
            pass

        if deep_sky_acquisitions:
            image_type = 'deep_sky'

            moon_age_list = []
            moon_illuminated_list = []

            dsa_data = {
                'dates': [],
                'frames': {},
                'integration': 0,
                'darks': [],
                'flats': [],
                'flat_darks': [],
                'bias': [],
                'bortle': [],
                'mean_sqm': [],
                'mean_fwhm': [],
                'temperature': [],
            }
            for a in deep_sky_acquisitions:
                if a.date is not None and a.date not in dsa_data['dates']:
                    dsa_data['dates'].append(a.date)
                    m = MoonPhase(a.date)
                    moon_age_list.append(m.age)
                    moon_illuminated_list.append(m.illuminated * 100.0)

                if a.number and a.duration:
                    key = ""
                    if a.filter:
                        key = "filter(%s)" % a.filter.get_name()
                    if a.iso:
                        key += '-ISO(%d)' % a.iso
                    if a.sensor_cooling:
                        key += '-temp(%d)' % a.sensor_cooling
                    if a.binning:
                        key += '-bin(%d)' % a.binning
                    key += '-duration(%d)' % a.duration

                    try:
                        current_frames = dsa_data['frames'][key]['integration']
                    except KeyError:
                        current_frames = '0x0"'

                    integration_re = re.match(r'^(\d+)x(\d+)"$', current_frames)
                    current_number = int(integration_re.group(1))

                    dsa_data['frames'][key] = {}
                    dsa_data['frames'][key]['filter_url'] = a.filter.get_absolute_url() if a.filter else '#'
                    dsa_data['frames'][key]['filter'] = a.filter if a.filter else ''
                    dsa_data['frames'][key]['iso'] = 'ISO%d' % a.iso if a.iso else ''
                    dsa_data['frames'][key]['sensor_cooling'] = '%dC' % a.sensor_cooling if a.sensor_cooling else ''
                    dsa_data['frames'][key]['binning'] = 'bin %sx%s' % (a.binning, a.binning) if a.binning else ''
                    dsa_data['frames'][key]['integration'] = '%sx%s"' % (current_number + a.number, a.duration)

                    dsa_data['integration'] += (a.duration * a.number / 3600.0)

                for i in ['darks', 'flats', 'flat_darks', 'bias']:
                    if a.filter and getattr(a, i):
                        dsa_data[i].append("%d" % getattr(a, i))
                    elif getattr(a, i):
                        dsa_data[i].append(getattr(a, i))

                if a.bortle:
                    dsa_data['bortle'].append(a.bortle)

                if a.mean_sqm:
                    dsa_data['mean_sqm'].append(a.mean_sqm)

                if a.mean_fwhm:
                    dsa_data['mean_fwhm'].append(a.mean_fwhm)

                if a.temperature:
                    dsa_data['temperature'].append(a.temperature)

            def average(values):
                if not len(values):
                    return 0
                return float(sum(values)) / len(values)

            frames_list = sorted(dsa_data['frames'].items())

            deep_sky_data = (
                (_('Resolution'), '%dx%d' % (image.w, image.h) if (image.w and image.h) else None),
                (_('Dates'), sorted(dsa_data['dates'])),
                (_('Frames'),
                    ('\n' if len(frames_list) > 1 else '') +
                    u'\n'.join("%s %s" % (
                        "<a href=\"%s\">%s</a>:" % (f[1]['filter_url'], f[1]['filter']) if f[1]['filter'] else '',
                        "%s %s %s %s" % (f[1]['integration'], f[1]['iso'], f[1]['sensor_cooling'], f[1]['binning']),
                    ) for f in frames_list)),
                (_('Integration'), "%.1f %s" % (dsa_data['integration'], _("hours"))),
                (_('Darks'), '~%d' % (int(reduce(lambda x, y: int(x) + int(y), dsa_data['darks'])) / len(dsa_data['darks'])) if dsa_data['darks'] else 0),
                (_('Flats'), '~%d' % (int(reduce(lambda x, y: int(x) + int(y), dsa_data['flats'])) / len(dsa_data['flats'])) if dsa_data['flats'] else 0),
                (_('Flat darks'), '~%d' % (int(reduce(lambda x, y: int(x) + int(y), dsa_data['flat_darks'])) / len(dsa_data['flat_darks'])) if dsa_data['flat_darks'] else 0),
                (_('Bias'), '~%d' % (int(reduce(lambda x, y: int(x) + int(y), dsa_data['bias'])) / len(dsa_data['bias'])) if dsa_data['bias'] else 0),
                (_('Avg. Moon age'), ("%.2f " % (average(moon_age_list), ) + _("days")) if moon_age_list else None),
                (_('Avg. Moon phase'), "%.2f%%" % (average(moon_illuminated_list), ) if moon_illuminated_list else None),
                (_('Bortle Dark-Sky Scale'), "%.2f" % (average([float(x) for x in dsa_data['bortle']])) if dsa_data['bortle'] else None),
                (_('Mean SQM'), "%.2f" % (average([float(x) for x in dsa_data['mean_sqm']])) if dsa_data['mean_sqm'] else None),
                (_('Mean FWHM'), "%.2f" % (average([float(x) for x in dsa_data['mean_fwhm']])) if dsa_data['mean_fwhm'] else None),
                (_('Temperature'),
                 "%.2f" % (average([float(x) for x in dsa_data['temperature']])) if dsa_data['temperature'] else None),
            )

        elif ssa:
            image_type = 'solar_system'



        profile = None
        if self.request.user.is_authenticated():
            profile = self.request.user.userprofile


        ##############
        # BASIC DATA #
        ##############

        updated_on = to_user_timezone(image.updated, profile) if profile else image.updated
        alias = 'regular'
        mod = self.request.GET.get('mod')
        if mod == 'inverted':
            alias = 'regular_inverted'

        subjects = image.objects_in_field.split(',') if image.objects_in_field else ''
        skyplot_zoom1 = None

        if is_revision:
            if revision_image.solution:
                if revision_image.solution.objects_in_field:
                   subjects = revision_image.solution.objects_in_field.split(',')
                if revision_image.solution.skyplot_zoom1:
                    skyplot_zoom1 = revision_image.solution.skyplot_zoom1
        else:
            if image.solution:
                if image.solution.objects_in_field:
                    subjects = image.solution.objects_in_field.split(',')
                if image.solution.skyplot_zoom1:
                    skyplot_zoom1 = image.solution.skyplot_zoom1

        subjects_limit = 5

        licenses = (
            (0, 'cc/c.png',           LICENSE_CHOICES[0][1]),
            (1, 'cc/cc-by-nc-sa.png', LICENSE_CHOICES[1][1]),
            (2, 'cc/cc-by-nc.png',    LICENSE_CHOICES[2][1]),
            (3, 'cc/cc-by-nc-nd.png', LICENSE_CHOICES[3][1]),
            (4, 'cc/cc-by.png',       LICENSE_CHOICES[4][1]),
            (5, 'cc/cc-by-sa.png',    LICENSE_CHOICES[5][1]),
            (6, 'cc/cc-by-nd.png',    LICENSE_CHOICES[6][1]),
        )

        locations = '; '.join([u'%s' % (x) for x in image.locations.all()])


        ######################
        # PREFERRED LANGUAGE #
        ######################

        preferred_language = image.user.userprofile.language
        if preferred_language:
            try:
                preferred_language = LANGUAGES[preferred_language]
            except KeyError:
                preferred_language = _("English")
        else:
            preferred_language = _("English")


        ##########################
        # LIKE / BOOKMARKED THIS #
        ##########################
        like_this = image.toggleproperties.filter(property_type = "like")
        bookmarked_this = image.toggleproperties.filter(property_type = "bookmark")


        ##############
        # NAVIGATION #
        ##############
        try:
            # Only lookup public images!
            image_next = Image.objects.filter(user = image.user, pk__gt = image.pk).order_by('pk')[0:1]
            image_prev = Image.objects.filter(user = image.user, pk__lt = image.pk).order_by('-pk')[0:1]
        except Image.DoesNotExist:
            image_next = None
            image_prev = None

        if image_next:
            image_next = image_next[0]
        if image_prev:
            image_prev = image_prev[0]


        ########
        # LIKE #
        ########
        from astrobin.context_processors import user_scores
        from astrobin_apps_premium.templatetags.astrobin_apps_premium_tags import is_free
        user_scores_index = user_scores(request)['user_scores_index']
        min_index_to_like = 1.00
        user_can_like = (
            request.user != image.user and
            (user_scores_index < 0 or user_scores_index >= min_index_to_like) or
            not is_free(request.user))


        #################
        # RESPONSE DICT #
        #################

        from astrobin_apps_platesolving.solver import Solver

        response_dict = context.copy()
        response_dict.update({
            'SHARE_PATH': settings.ASTROBIN_SHORT_BASE_URL,

            'alias': alias,
            'mod': mod,
            'revisions': ImageRevision.objects.select_related('image__user__userprofile').filter(image = image),
            'is_revision': is_revision,
            'revision_image': revision_image,
            'revision_label': r,

            'instance_to_platesolve': instance_to_platesolve,
            'show_solution': instance_to_platesolve.solution and instance_to_platesolve.solution.status == Solver.SUCCESS,
            'skyplot_zoom1': skyplot_zoom1,

            'image_ct': ContentType.objects.get_for_model(Image),
            'like_this': like_this,
            'user_can_like': user_can_like,
            'bookmarked_this': bookmarked_this,
            'min_index_to_like': min_index_to_like,

            'comments_number': NestedComment.objects.filter(
                deleted = False,
                content_type__app_label = 'astrobin',
                content_type__model = 'image',
                object_id = image.id).count(),
            'gear_list': gear_list,
            'makes_list': makes_list,
            'gear_list_has_commercial': gear_list_has_commercial,
            'gear_list_has_paid_commercial': gear_list_has_paid_commercial,
            'image_type': image_type,
            'ssa': ssa,
            'deep_sky_data': deep_sky_data,
            # TODO: check that solved image is correcly laid on top
            'private_message_form': PrivateMessageForm(),
            'upload_revision_form': ImageRevisionUploadForm(),
            'dates_label': _("Dates"),
            'updated_on': updated_on,
            'show_contains': (image.subject_type == 100 and subjects) or (image.subject_type >= 200),
            'subjects_short': subjects[:subjects_limit],
            'subjects_reminder': subjects[subjects_limit:],
            'subjects_all': subjects,
            'subjects_limit': subjects_limit,
            'subject_type': [x[1] for x in Image.SUBJECT_TYPE_CHOICES if x[0] == image.subject_type][0] if image.subject_type else 0,
            'license_icon': licenses[image.license][1],
            'license_title': licenses[image.license][2],
            'locations': locations,
            # Because of a regression introduced at
            # revision e1dad12babe5, now we have to
            # implement this ugly hack.

            'solar_system_main_subject_id': image.solar_system_main_subject,
            'solar_system_main_subject': SOLAR_SYSTEM_SUBJECT_CHOICES[image.solar_system_main_subject][1] if image.solar_system_main_subject is not None else None,
            'content_type': ContentType.objects.get(app_label = 'astrobin', model = 'image'),
            'preferred_language': preferred_language,
            'select_datapool_form': PublicDataPool_SelectExistingForm(),
            'select_sharedfolder_form': PrivateSharedFolder_SelectExistingForm(user = self.request.user) if self.request.user.is_authenticated() else None,
            'has_sharedfolders': PrivateSharedFolder.objects.filter(
                Q(creator = self.request.user) |
                Q(users = self.request.user)).count() > 0 if self.request.user.is_authenticated() else False,

            'iotd_date': image.iotd_date(),
            'image_next': image_next,
            'image_prev': image_prev,
        })

        return response_dict