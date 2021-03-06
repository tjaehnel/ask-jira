from __future__ import print_function
from __future__ import unicode_literals

import unicodedata
from io import BytesIO
from jira.client import JIRA
from jira.exceptions import JIRAError

def export_import_issues(source_jira, conf, query):
    target_jira = JIRA({'server': conf.JIRA['server']},
                basic_auth=(conf.JIRA['user'], conf.JIRA['password']))
    issues = source_jira.search_issues(query, maxResults=False)
    result = []
    print('About to export/import', len(issues), 'issues')
    _make_new_issues(source_jira, target_jira, issues, conf, result, None)
    return result

_g_issue_map = {}
_g_subtask_map = {}

def _make_new_issues(source_jira, target_jira, issues, conf, result, parent):
    global _g_subtask_map
    no_auto_create_subtasks = getattr(conf,'NO_AUTO_CREATE_SUBTASKS',False) 
    issue_number=0
    for issue in issues:
        issue_number = issue_number + 1
        if no_auto_create_subtasks:
            if issue.key in _g_subtask_map:
                parent = _g_subtask_map[issue.key]
            else:
                parent = None
            print('Exporting', issue.key, end=' ')
        else:
            if not parent:
                print('Exporting', issue.key, end=' ')

        # re-fetch to include comments and attachments
        issue = source_jira.issue(issue.key, expand='comments,attachments')
        fields = _get_new_issue_fields(issue.fields, conf)

        if no_auto_create_subtasks:
            if fields['issuetype']['name'] == 'Sub-task' and not parent:
                print('\n     WARNING: %s is a "Sub-task" but the parent has not been created, yet' % issue.key)
                print('     Creating it as regular "Task"')
                print('     -> please manually convert it into a "Sub-task" and link it under the appropriate Issue')
                fields['issuetype']['name'] = 'Task'
            if parent:
                print('(as a subtask of %s)' % parent.key, end=' ')
        if parent:
            fields['parent'] = {'key': parent.key}
 
        # Migrate issue version.
        source_versions = getattr(issue.fields, 'fixVersions')
        if source_versions is not None:
            target_versions = []
            for version in source_versions:
                # We create the current version if it does not exist in the target JIRA project.
                target_version = _get_target_version_by_name(target_jira, conf, getattr(version, 'name'))
                if target_version is None:
                    target_version = target_jira.create_version(getattr(version, 'name'), conf.JIRA['project'])

                target_versions.append({'id': getattr(target_version, 'id')})

            # Support multiple versions per ticket.
            fields['fixVersions'] = target_versions

        # Migrate issue components.
        source_components = getattr(issue.fields, 'components')
        if source_components is not None:
            target_components = []
            for component in source_components:
                # We create the current component if it does not exist in the target JIRA project.
                target_component = _get_target_component_by_name(target_jira, conf, getattr(component, 'name'))
                if target_component is None:
                    target_component = target_jira.create_component(getattr(component, 'name'), conf.JIRA['project'])

                target_components.append({'id': getattr(target_component, 'id')})

            # Support multiple components per ticket.
            fields['components'] = target_components


        if getattr(conf, 'FILL_CONSECUTIVE_NUMBERING_GAPS', False) and getattr(conf, 'NO_AUTO_CREATE_EPICS', False) and no_auto_create_subtasks:
            dummy_fields = { }
            dummy_fields['project'] = fields['project']
            dummy_fields['issuetype'] = getattr(conf, 'FILL_CONSECUTIVE_DUMMY_ISSUETYPE', 'Task')
            dummy_fields['summary'] = "DUMMY Issue created during import to preserve issue numbering"
#            print(dummy_fields)
            source_issue_number=int(issue.key.split('-')[1])
            while(source_issue_number > issue_number):
                print('Issue IDs in source Jira are not consecutive!')
                print('  - Want to create issue ', source_issue_number, ' but missing preceding issue ', issue_number, '.')
                print('  -> Creating a dummy issue...')
                target_jira.create_issue(fields=dummy_fields)
                issue_number = issue_number + 1

#        print(fields)
        new_issue = target_jira.create_issue(fields=fields)
        _g_issue_map[issue.key] = new_issue
        if not parent or no_auto_create_subtasks:
            print('to', new_issue.key, '...', end=' ')

        if getattr(conf,'NO_AUTO_CREATE_EPICS',False):
            _set_epic_link_nocreate(new_issue, issue, conf, source_jira, target_jira)
        else:
            _set_epic_link(new_issue, issue, conf, source_jira, target_jira)
        _set_status(new_issue, issue, conf, target_jira)

        if issue.fields.worklog:
            for worklog in issue.fields.worklog.worklogs:
                target_jira.add_worklog(new_issue, None, worklog.timeSpentSeconds)

        if issue.fields.comment.comments:
            _add_comments(new_issue, target_jira, issue.fields.comment.comments)
        if issue.fields.attachment:
            try:
                _add_attachments(new_issue, target_jira, issue.fields.attachment)
            except JIRAError as e:
                print('ERROR: attachment import failed with status',
                        e.status_code, '...', end=' ')
                target_jira.add_comment(new_issue, '*Failed to import attachments*')
        if issue.fields.subtasks:
            if no_auto_create_subtasks:
                for subtaskid in issue.fields.subtasks:
                    _g_subtask_map[subtaskid.key] = new_issue
            else:
                subtasks = [source_jira.issue(subtask.key) for subtask in
                        issue.fields.subtasks]
                print('with', len(subtasks), 'subtasks ...', end=' ')
                _make_new_issues(source_jira, target_jira, subtasks, conf, result, new_issue)

        comment = 'Imported from *[{1}|{0}/browse/{1}]*'.format(
                source_jira._options['server'], issue.key)
        target_jira.add_comment(new_issue, comment)
        if conf.ADD_COMMENT_TO_OLD_ISSUE:
            comment = 'Exported to *[{1}|{0}/browse/{1}]*'.format(
                    target_jira._options['server'], new_issue.key)
            source_jira.add_comment(issue, comment)

        result.append(new_issue.key)
        if not parent or no_auto_create_subtasks:
            print('done')


def _get_target_version_by_name(jira, conf, name):
    """
    Get an existing version by name for the current project.

    :param jira: current jira resource
    :param conf: JIRA configurations
    :param name: name of the version to check
    """
    versions = jira.project_versions(conf.JIRA['project'])
    for version in versions:
        if getattr(version, 'name') == name:
            return version

    return None


def _get_target_component_by_name(jira, conf, name):
    """
    Get an existing component by name for the current project.

    :param jira: current jira resource
    :param conf: JIRA configurations
    :param name: name of the component to check
    """
    components = jira.project_components(conf.JIRA['project'])
    for component in components:
        if getattr(component, 'name') == name:
            return component

    return None

def _get_new_issue_fields(fields, conf):
    result = {}
    result['project'] = conf.JIRA['project']
    for name in ('summary', 'description', 'labels'):
        value = getattr(fields, name)
        if value is not None:
            result[name] = value
    for name in ('priority', 'issuetype', 'assignee', 'reporter'):
        value = getattr(fields, name)
        if value:
            value = getattr(value, 'name')
            name_map = getattr(conf, name.upper() + '_MAP')
            result[name] = {'name': name_map[value]}
    if conf.CUSTOM_FIELD:
        result[conf.CUSTOM_FIELD[0]] = conf.CUSTOM_FIELD[1]
    if conf.CUSTOM_FIELD_MAP:
        for sourcename in conf.CUSTOM_FIELD_MAP.keys():
            targetname = conf.CUSTOM_FIELD_MAP[sourcename]
            value = getattr(fields, sourcename, None)
            if value:
                result[targetname] = value
    custom_field_static_issuetype= getattr(conf, "CUSTOM_FIELD_STATIC_ISSUETYPE", None)
    custom_field_static = getattr(conf, "CUSTOM_FIELD_STATIC", None)
    issue_type = result['issuetype']['name']
    if custom_field_static_issuetype:
        if issue_type in custom_field_static_issuetype:
            custom_field_static = custom_field_static_issuetype[issue_type]
    
    if custom_field_static:
        for sourcename in custom_field_static.keys():
            value = custom_field_static[sourcename]
            if value:
                result[sourcename] = value
    epicName = getattr(fields, conf.SOURCE_EPIC_NAME_FIELD_ID, None)
    if epicName:
        result[conf.TARGET_EPIC_NAME_FIELD_ID] = epicName

    return result

_g_epic_map = {}

def _set_epic_link_nocreate(new_issue, old_issue, conf, source_jira, target_jira):
    source_epic_key = getattr(old_issue.fields, conf.SOURCE_EPIC_LINK_FIELD_ID)
    if not source_epic_key:
        return
    global _g_issue_map
    if source_epic_key not in _g_issue_map:
        print('\n     WARNING: Epic %s (source ID) has not been created yet so cannot link Story %s (target ID) to it' % (source_epic_key, new_issue.key))
        print('     -> please create this link manually')
    else:
        target_epic = _g_issue_map[source_epic_key]
        target_jira.add_issues_to_epic(target_epic.key, [new_issue.key])
        print('linked to epic', target_epic.key, '...', end=' ')

def _set_epic_link(new_issue, old_issue, conf, source_jira, target_jira):
    source_epic_key = getattr(old_issue.fields, conf.SOURCE_EPIC_LINK_FIELD_ID)
    if not source_epic_key:
        return
    global _g_epic_map
    if source_epic_key not in _g_epic_map:
        source_epic = source_jira.issue(source_epic_key)
        epic_fields = _get_new_issue_fields(source_epic.fields, conf)
        epic_fields[conf.TARGET_EPIC_NAME_FIELD_ID] = getattr(
                source_epic.fields, conf.SOURCE_EPIC_NAME_FIELD_ID)
        target_epic = target_jira.create_issue(fields=epic_fields)
        _g_epic_map[source_epic_key] = target_epic
    target_epic = _g_epic_map[source_epic_key]
    target_jira.add_issues_to_epic(target_epic.key, [new_issue.key])
    print('linked to epic', target_epic.key, '...', end=' ')

def _set_status(new_issue, old_issue, conf, target_jira):
    issue_type = new_issue.fields.issuetype.name
    status_name = old_issue.fields.status.name

    transitions = None
    transition_map = getattr(conf, "STATUS_TRANSITIONS_ISSUETYPE", None)
    if transition_map:
        print("Looking for transitions for Issue Type: ",issue_type)
        if issue_type in transition_map:
            print("Using specific transition map")
            transition_map = transition_map[issue_type]
        else:
            transition_map = None
    if not transition_map:
        print("Using generic transition map")
        transition_map = conf.STATUS_TRANSITIONS

    transitions = transition_map[status_name]
    print("Transition to ", status_name, " (source status) via ", transitions)
    if not transitions:
        return
    for transition_name in transitions:
        if isinstance(transition_name, conf.WithResolution):
            resolution = conf.RESOLUTION_MAP[old_issue.fields.resolution.name]
            print("Performing transition: ", transition_name, " with resolution: ", resolution)
            target_jira.transition_issue(new_issue, transition_name.transition_name,
                    fields={'resolution': {'name': resolution}})
        else:
            print("Performing transition: ", transition_name)
            target_jira.transition_issue(new_issue, transition_name)

def _add_comments(issue, jira, comments):
    for comment in comments:
        jira.add_comment(issue, u"*Comment by {0}*:\n{1}"
                .format(comment.author.displayName, comment.body))

def _add_attachments(issue, jira, attachments):
    for attachment in attachments:
        with BytesIO() as buf:
            for chunk in attachment.iter_content():
                buf.write(chunk)
            jira.add_attachment(issue,
                    filename=_normalize_filename(attachment.filename),
                    attachment=buf)

def _normalize_filename(value):
    return unicodedata.normalize('NFKD', value).encode('ascii',
            'ignore').decode('ascii')

# -------------------------------------
# TODO:
# - more mappings from http://stackoverflow.com/a/26043914/258772
# - components, fixVersions (use create_version())
# - estimates and timelogs
#   - tried, no luck, even though seems to have been working:
#     https://answers.atlassian.com/questions/211138/defining-original-estimation-value-via-api

# Not doing:
# - keep original key: JIRA does not support this
# - comment authors map -- cannot change comment authors easily, Google for
#   reasons
