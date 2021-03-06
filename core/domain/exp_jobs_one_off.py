# coding: utf-8
#
# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""One-off jobs for explorations."""

from __future__ import absolute_import  # pylint: disable=import-only-modules
from __future__ import unicode_literals  # pylint: disable=import-only-modules

import ast
import logging

from constants import constants
from core import jobs
from core.domain import exp_domain
from core.domain import exp_fetchers
from core.domain import exp_services
from core.domain import html_validation_service
from core.domain import rights_manager
from core.platform import models
import feconf
import python_utils
import utils

(exp_models,) = models.Registry.import_models([
    models.NAMES.exploration])


class ExplorationFirstPublishedOneOffJob(jobs.BaseMapReduceOneOffJobManager):
    """One-off job that finds first published time in milliseconds for all
    explorations.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationRightsSnapshotContentModel]

    @staticmethod
    def map(item):
        if item.content['status'] == rights_manager.ACTIVITY_STATUS_PUBLIC:
            yield (
                item.get_unversioned_instance_id(),
                utils.get_time_in_millisecs(item.created_on))

    @staticmethod
    def reduce(exp_id, stringified_commit_times_msecs):
        exploration_rights = rights_manager.get_exploration_rights(
            exp_id, strict=False)
        if exploration_rights is None:
            return

        commit_times_msecs = [
            ast.literal_eval(commit_time_string) for
            commit_time_string in stringified_commit_times_msecs]
        first_published_msec = min(commit_times_msecs)
        rights_manager.update_activity_first_published_msec(
            constants.ACTIVITY_TYPE_EXPLORATION, exp_id,
            first_published_msec)


class ExplorationValidityJobManager(jobs.BaseMapReduceOneOffJobManager):
    """Job that checks that all explorations have appropriate validation
    statuses.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return

        exploration = exp_fetchers.get_exploration_from_model(item)
        exp_rights = rights_manager.get_exploration_rights(item.id)

        try:
            if exp_rights.status == rights_manager.ACTIVITY_STATUS_PRIVATE:
                exploration.validate()
            else:
                exploration.validate(strict=True)
        except utils.ValidationError as e:
            yield (item.id, python_utils.convert_to_bytes(e))

    @staticmethod
    def reduce(key, values):
        yield (key, values)


class ExplorationMigrationAuditJob(jobs.BaseMapReduceOneOffJobManager):
    """A reusable one-off job for testing exploration migration from any
    exploration schema version to the latest. This job runs the state
    migration, but does not commit the new exploration to the store.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @classmethod
    def enqueue(cls, job_id, additional_job_params=None):
        super(ExplorationMigrationAuditJob, cls).enqueue(
            job_id, shard_count=64)

    @staticmethod
    def map(item):
        if item.deleted:
            return

        current_state_schema_version = feconf.CURRENT_STATE_SCHEMA_VERSION

        states_schema_version = item.states_schema_version
        versioned_exploration_states = {
            'states_schema_version': states_schema_version,
            'states': item.states
        }
        while states_schema_version < current_state_schema_version:
            try:
                exp_domain.Exploration.update_states_from_model(
                    versioned_exploration_states, states_schema_version,
                    item.id)
                states_schema_version += 1
            except Exception as e:
                error_message = (
                    'Exploration %s failed migration to states v%s: %s' %
                    (item.id, states_schema_version + 1, e))
                logging.exception(error_message)
                yield ('MIGRATION_ERROR', error_message.encode('utf-8'))
                break

            if states_schema_version == current_state_schema_version:
                yield ('SUCCESS', 1)

    @staticmethod
    def reduce(key, values):
        if key == 'SUCCESS':
            yield (key, len(values))
        else:
            yield (key, values)


class ExplorationMigrationJobManager(jobs.BaseMapReduceOneOffJobManager):
    """A reusable one-time job that may be used to migrate exploration schema
    versions. This job will load all existing explorations from the data store
    and immediately store them back into the data store. The loading process of
    an exploration in exp_services automatically performs schema updating. This
    job persists that conversion work, keeping explorations up-to-date and
    improving the load time of new explorations.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @classmethod
    def enqueue(cls, job_id, additional_job_params=None):
        super(ExplorationMigrationJobManager, cls).enqueue(
            job_id, shard_count=64)

    @staticmethod
    def map(item):
        if item.deleted:
            return

        # Do not upgrade explorations that fail non-strict validation.
        old_exploration = exp_fetchers.get_exploration_by_id(item.id)
        try:
            old_exploration.validate()
        except Exception as e:
            logging.error(
                'Exploration %s failed non-strict validation: %s' %
                (item.id, e))
            return

        # If the exploration model being stored in the datastore is not the
        # most up-to-date states schema version, then update it.
        if (item.states_schema_version !=
                feconf.CURRENT_STATE_SCHEMA_VERSION):
            # Note: update_exploration does not need to apply a change list in
            # order to perform a migration. See the related comment in
            # exp_services.apply_change_list for more information.
            #
            # Note: from_version and to_version really should be int, but left
            # as str to conform with legacy data.
            commit_cmds = [exp_domain.ExplorationChange({
                'cmd': exp_domain.CMD_MIGRATE_STATES_SCHEMA_TO_LATEST_VERSION,
                'from_version': python_utils.UNICODE(
                    item.states_schema_version),
                'to_version': python_utils.UNICODE(
                    feconf.CURRENT_STATE_SCHEMA_VERSION)
            })]
            exp_services.update_exploration(
                feconf.MIGRATION_BOT_USERNAME, item.id, commit_cmds,
                'Update exploration states from schema version %d to %d.' % (
                    item.states_schema_version,
                    feconf.CURRENT_STATE_SCHEMA_VERSION))
            yield ('SUCCESS', item.id)

    @staticmethod
    def reduce(key, values):
        yield (key, len(values))


class ExplorationMathSvgFilenameValidationOneOffJob(
        jobs.BaseMapReduceOneOffJobManager):
    """Job that checks the html content of an exploration and validates the
    svg_filename fields in each math rich-text components.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return

        exploration = exp_fetchers.get_exploration_from_model(item)
        invalid_tags_info_in_exp = []
        for state_name, state in exploration.states.items():
            html_string = ''.join(state.get_all_html_content_strings())
            error_list = (
                html_validation_service.
                validate_svg_filenames_in_math_rich_text(
                    feconf.ENTITY_TYPE_EXPLORATION, item.id, html_string))
            if len(error_list) > 0:
                invalid_tags_info_in_state = {
                    'state_name': state_name,
                    'error_list': error_list,
                    'no_of_invalid_tags': len(error_list)
                }
                invalid_tags_info_in_exp.append(invalid_tags_info_in_state)
        if len(invalid_tags_info_in_exp) > 0:
            yield ('Found invalid tags', (item.id, invalid_tags_info_in_exp))

    @staticmethod
    def reduce(key, values):
        final_values = [ast.literal_eval(value) for value in values]
        no_of_invalid_tags = 0
        invalid_tags_info = {}
        for exp_id, invalid_tags_info_in_exp in final_values:
            invalid_tags_info[exp_id] = []
            for value in invalid_tags_info_in_exp:
                no_of_invalid_tags += value['no_of_invalid_tags']
                del value['no_of_invalid_tags']
                invalid_tags_info[exp_id].append(value)

        final_value_dict = {
            'no_of_explorations_with_no_svgs': len(final_values),
            'no_of_invalid_tags': no_of_invalid_tags,
        }
        yield ('Overall result.', final_value_dict)
        yield ('Detailed information on invalid tags. ', invalid_tags_info)


class ExplorationMockMathMigrationOneOffJob(jobs.BaseMapReduceOneOffJobManager):
    """Job that migrates all the math tags in the exploration to the new schema
    but does not save the migrated exploration. The new schema has the attribute
    math-content-with-value which includes a field for storing reference to
    SVGs. This job is used to verify that the actual migration will be possible
    for all the explorations.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return

        exploration = exp_fetchers.get_exploration_from_model(item)
        exploration_status = (
            rights_manager.get_exploration_rights(
                item.id).status)
        for state_name, state in exploration.states.items():
            html_string = ''.join(
                state.get_all_html_content_strings())

            converted_html_string = (
                html_validation_service.add_math_content_to_math_rte_components(
                    html_string))

            error_list = (
                html_validation_service.
                validate_math_tags_in_html_with_attribute_math_content(
                    converted_html_string))
            if len(error_list) > 0:
                key = (
                    'exp_id: %s, exp_status: %s failed validation after '
                    'migration' % (
                        item.id, exploration_status))
                value_dict = {
                    'state_name': state_name,
                    'error_list': error_list,
                    'no_of_invalid_tags': len(error_list)
                }
                yield (key, value_dict)

    @staticmethod
    def reduce(key, values):
        yield (key, values)


class ExplorationMathRichTextInfoModelGenerationOneOffJob(
        jobs.BaseMapReduceOneOffJobManager):
    """Job that finds all the explorations with math rich text components and
    creates a temporary storage model with all the information required for
    generating math rich text component SVG images.
    """

    # A constant that will be yielded as a key by this job in the map function,
    # When it finds an exploration with math rich text components without SVGs.
    _SUCCESS_KEY = 'exploration-with-math-tags'

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return

        exploration = exp_fetchers.get_exploration_from_model(item)
        try:
            exploration.validate()
        except Exception as e:
            logging.error(
                'Exploration %s failed non-strict validation: %s' %
                (item.id, e))
            yield (
                'validation_error',
                'Exploration %s failed non-strict validation: %s' %
                (item.id, e))
            return
        html_strings_in_exploration = ''
        for state in exploration.states.values():
            html_strings_in_exploration += (
                ''.join(state.get_all_html_content_strings()))
        list_of_latex_strings_without_svg = (
            html_validation_service.
            get_latex_strings_without_svg_from_html(
                html_strings_in_exploration))
        if len(list_of_latex_strings_without_svg) > 0:
            yield (
                ExplorationMathRichTextInfoModelGenerationOneOffJob.
                _SUCCESS_KEY,
                (item.id, list_of_latex_strings_without_svg))

    @staticmethod
    def reduce(key, values):
        if key == (
                ExplorationMathRichTextInfoModelGenerationOneOffJob.
                _SUCCESS_KEY):
            final_values = [ast.literal_eval(value) for value in values]
            estimated_no_of_batches = 1
            approx_size_of_math_svgs_bytes_in_current_batch = 0
            exploration_math_rich_text_info_list = []
            longest_raw_latex_string = ''
            total_number_of_svgs_required = 0
            for exp_id, list_of_latex_strings_without_svg in final_values:
                math_rich_text_info = (
                    exp_domain.ExplorationMathRichTextInfo(
                        exp_id, True, list_of_latex_strings_without_svg))
                exploration_math_rich_text_info_list.append(
                    math_rich_text_info)

                approx_size_of_math_svgs_bytes = (
                    math_rich_text_info.get_svg_size_in_bytes())
                total_number_of_svgs_required += len(
                    list_of_latex_strings_without_svg)
                longest_raw_latex_string = max(
                    math_rich_text_info.get_longest_latex_expression(),
                    longest_raw_latex_string, key=len)
                approx_size_of_math_svgs_bytes_in_current_batch += int(
                    approx_size_of_math_svgs_bytes)
                if approx_size_of_math_svgs_bytes_in_current_batch > (
                        feconf.MAX_SIZE_OF_MATH_SVGS_BATCH_BYTES):
                    approx_size_of_math_svgs_bytes_in_current_batch = 0
                    estimated_no_of_batches += 1

            exp_services.save_multi_exploration_math_rich_text_info_model(
                exploration_math_rich_text_info_list)

            final_value_dict = {
                'estimated_no_of_batches': estimated_no_of_batches,
                'longest_raw_latex_string': longest_raw_latex_string,
                'number_of_explorations_having_math': (
                    len(final_values)),
                'total_number_of_svgs_required': total_number_of_svgs_required
            }
            yield (key, final_value_dict)
        else:
            yield (key, values)


class ExplorationMathRichTextInfoModelDeletionOneOffJob(
        jobs.BaseMapReduceOneOffJobManager):
    """Job that deletes all instances of the ExplorationMathRichTextInfoModel
    from the datastore.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationMathRichTextInfoModel]

    @staticmethod
    def map(item):
        item.delete()
        yield ('model_deleted', 1)

    @staticmethod
    def reduce(key, values):
        no_of_models_deleted = (
            sum(ast.literal_eval(v) for v in values))
        yield (key, ['%d models successfully delelted.' % (
            no_of_models_deleted)])


class ViewableExplorationsAuditJob(jobs.BaseMapReduceOneOffJobManager):
    """Job that outputs a list of private explorations which are viewable."""

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return

        exploration_rights = rights_manager.get_exploration_rights(
            item.id, strict=False)
        if exploration_rights is None:
            return

        if (exploration_rights.status == constants.ACTIVITY_STATUS_PRIVATE
                and exploration_rights.viewable_if_private):
            yield (item.id, item.title.encode('utf-8'))

    @staticmethod
    def reduce(key, values):
        yield (key, values)


class HintsAuditOneOffJob(jobs.BaseMapReduceOneOffJobManager):
    """Job that tabulates the number of hints used by each state of an
    exploration.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return

        exploration = exp_fetchers.get_exploration_from_model(item)
        for state_name, state in exploration.states.items():
            hints_length = len(state.interaction.hints)
            if hints_length > 0:
                exp_and_state_key = '%s %s' % (
                    item.id, state_name.encode('utf-8'))
                yield (python_utils.UNICODE(hints_length), exp_and_state_key)

    @staticmethod
    def reduce(key, values):
        yield (key, values)


class ExplorationContentValidationJobForCKEditor(
        jobs.BaseMapReduceOneOffJobManager):
    """Job that checks the html content of an exploration and validates it
    for CKEditor.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return

        try:
            exploration = exp_fetchers.get_exploration_from_model(item)
        except Exception as e:
            yield (
                'Error %s when loading exploration'
                % python_utils.convert_to_bytes(e), [item.id])
            return

        html_list = exploration.get_all_html_content_strings()

        err_dict = html_validation_service.validate_rte_format(
            html_list, feconf.RTE_FORMAT_CKEDITOR)

        for key in err_dict:
            if err_dict[key]:
                yield ('%s Exp Id: %s' % (key, item.id), err_dict[key])

    @staticmethod
    def reduce(key, values):
        final_values = [ast.literal_eval(value) for value in values]
        # Combine all values from multiple lists into a single list
        # for that error type.
        output_values = list(set().union(*final_values))
        exp_id_index = key.find('Exp Id:')
        if exp_id_index == -1:
            yield (key, output_values)
        else:
            output_values.append(key[exp_id_index:])
            yield (key[:exp_id_index - 1], output_values)


class RTECustomizationArgsValidationOneOffJob(
        jobs.BaseMapReduceOneOffJobManager):
    """One-off job for validating all the customizations arguments of
    Rich Text Components.
    """

    @classmethod
    def entity_classes_to_map_over(cls):
        return [exp_models.ExplorationModel]

    @staticmethod
    def map(item):
        if item.deleted:
            return
        err_dict = {}

        try:
            exploration = exp_fetchers.get_exploration_from_model(item)
        except Exception as e:
            yield (
                'Error %s when loading exploration'
                % python_utils.UNICODE(e), [item.id])
            return

        html_list = exploration.get_all_html_content_strings()
        err_dict = html_validation_service.validate_customization_args(
            html_list)
        for key in err_dict:
            err_value_with_exp_id = err_dict[key]
            err_value_with_exp_id.append('Exp ID: %s' % item.id)
            yield (key, err_value_with_exp_id)

    @staticmethod
    def reduce(key, values):
        final_values = [ast.literal_eval(value) for value in values]
        flattened_values = [
            item for sublist in final_values for item in sublist]

        # Errors produced while loading exploration only contain exploration id
        # in error message, so no further formatting is required. For errors
        # from validation the output is in format [err1, expid1, err2, expid2].
        # So, we further format it as [(expid1, err1), (expid2, err2)].
        if 'loading exploration' in key:
            yield (key, flattened_values)
            return

        output_values = []
        index = 0
        while index < len(flattened_values):
            # flattened_values[index] = error message.
            # flattened_values[index + 1] = exp id in which error message
            # is present.
            output_values.append((
                flattened_values[index + 1], flattened_values[index]))
            index += 2
        output_values.sort()
        yield (key, output_values)
