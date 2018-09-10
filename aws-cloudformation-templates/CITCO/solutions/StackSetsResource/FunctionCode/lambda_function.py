"""
StackSet via CloudFormation
"""

# Next ToDo:
# Allow for stack Retention

import boto3
from time import sleep
from botocore.exceptions import ClientError
import os

import crhelper

# initialise logger
logger = crhelper.log_config({"RequestId": "CONTAINER_INIT"})
logger.info('Logging configured')
# set global to track init failures
init_failed = False

try:
    # Place initialization code here
    logger.info("Container initialization completed")
except Exception as e:
    logger.error(e, exc_info=True)
    init_failed = e


def get_stack_from_arn(arn):
    # Given the ARN of a CloudFormation stack, return the stack name
    (arn, partion, service, region, account, resourcepart) = arn.split(':', 5)
    if ':' in resourcepart:
        (resourcetype, resource) = resourcepart.split(':')
    elif '/' in resourcepart:
        resourceparts = resourcepart.split('/')
        resource = resourceparts[1]
    else:
        (resource) = resourcepart
    return(resource)


def change_requires_update(attributes, old_values, current_values):
    # Given a list of attributes, compare the old and new values to see if
    # there's been a change.
    for attribute in attributes:
        if (attribute not in old_values) and (attribute in current_values):
            logger.debug("New value for %s: %s" % (attribute, current_values[attribute]))
            return True
        if (attribute in old_values) and (attribute not in current_values):
            logger.debug("Value removed for %s: %s" % (attribute, old_values[attribute]))
            return True
        if (attribute in old_values) and (attribute in current_values):
            logger.debug("Evaluating %s: %s vs. %s" % (attribute, current_values[attribute], old_values[attribute]))
            if current_values[attribute] != old_values[attribute]:
                return True
    return False


def convert_ops_prefs(ops_prefs):
    # CloudFormation parameters are all strings.  We need to convert numeric
    # values in the ops_prefs JSON object to ints before we can call the API
    logger.info("Converting Operation Preferences values")
    converted_ops_prefs = {}
    needs_conversion = set(['FailureToleranceCount', 'FailureTolerancePercentage', 'MaxConcurrentCount', 'MaxConcurrentPercentage'])
    for key, value in ops_prefs.items():
        logger.debug("Evaluating %s : %s" % (key, value))
        if key in needs_conversion:
            logger.debug("Converting %s" % key)
            converted_ops_prefs[key] = int(value)
        elif key == 'RegionOrder':
            converted_ops_prefs['RegionOrder'] = value
        else:
            logger.warning("Warning: Skipping unknown key: %s in Operation Preferences" % key)
    return(converted_ops_prefs)


def expand_tags(tags):
    # We get the Tags as a list of key, value pairs, but CloudFormation needs
    # them exploded out to Key: key, Value: value.
    tags_array = []
    for tag in tags:
        logger.debug(tag)
        key, value = list(tag.items())[0]
        tags_array.append({'Key': key, 'Value': value})
    return(tags_array)


def expand_parameters(params):
    # We get the Parameters as a list of key, value pairs, but CloudFormation
    # needs them exploded out to ParameterKey: key, ParameterValue: value.
    params_array = []
    for param in params:
        logger.debug(param)
        key, value = list(param.items())[0]
        params_array.append({'ParameterKey': key, 'ParameterValue': value})
    return(params_array)


def flatten_stacks(stackinstances):
    # Stack instances are defined across accounts and regions + parameter
    # overrides.  We want to expand all combinations before we take action.
    flat_stacks = {}
    for instance in stackinstances:
        for account in instance['Accounts']:
            for region in instance['Regions']:
                tuple = ("%s/%s" % (account, region))
                if tuple in flat_stacks:
                    raise Exception("%s / %s is defined multiple times" % (account, region))
                if 'ParameterOverrides' in instance:
                    flat_stacks[tuple] = instance['ParameterOverrides']
                else:
                    flat_stacks[tuple] = []
    return(flat_stacks)


def group_by_account(set, flat_stacks):
    # Group regions by account and overrides
    grouped_accounts = {}
    for instance in set:
        account, region = instance.split('/')
        if account in grouped_accounts:
            if flat_stacks[instance] == grouped_accounts[account]['overrides']:
                grouped_accounts[account]['regions'].append(region)
            else:
                raise Exception("The overrides didn't match account group for %s" % instance)
        else:
            grouped_accounts[account] = {'regions': [region],
                                         'overrides': flat_stacks[instance]}
    return(grouped_accounts)


def aggregate_instances(account_list, flat_stacks):
    # First group regions by account and overrides
    accounts = group_by_account(account_list, flat_stacks)

    # Aggregate accounts into instances with similar regions to reduce number
    # of API calls
    instances = []
    while accounts.keys():
        instance = {}
        aggregated_accounts = []
        (source_account, values) = accounts.popitem()
        for account in accounts:
            if accounts[account] == values:
                aggregated_accounts.append(account)
        for account in aggregated_accounts:
            accounts.pop(account)
        aggregated_accounts.append(source_account)
        instance = {'accounts': aggregated_accounts,
                    'regions': values['regions'],
                    'overrides': values['overrides']}
        instances.append(instance)
    logger.debug(instances)
    return(instances)


def launch_stacks(set_region, set_name, accts, regions, param_overrides,
                  ops_prefs):
    # Wrapper for create_stack_instances
    sleep_time = 15
    retries = 20
    this_try = 0

    logger.info("Creating stacks with op prefs %s" % ops_prefs)
    logger.debug("StackSetName: %s, Accounts: %s, Regions: %s, ParameterOverrides: %s" % (set_name, accts, regions, param_overrides))

    while True:
        try:
            client = boto3.client('cloudformation', region_name=set_region)
            response = client.create_stack_instances(
                StackSetName=set_name,
                Accounts=accts,
                Regions=regions,
                ParameterOverrides=param_overrides,
                OperationPreferences=ops_prefs,
                # OperationId='string'
            )
            return(response)
        except ClientError as e:
            if e.response['Error']['Code'] == 'OperationInProgressException':
                this_try += 1
                if this_try == retries:
                    return("Failed to launch stacks after %s tries" % this_try)
                else:
                    logger.warning("Operation in progress for %s in %s. Sleeping for %i seconds." % (set_name, set_region, sleep_time))
                    sleep(sleep_time)
                    continue
            elif e.response['Error']['Code'] == 'StackSetNotFoundException':
                raise Exception("No StackSet matching %s found in %s. You must create before launching stacks." % (set_name, set_region))
            else:
                raise Exception("Error launching stack instance: %s" % e)


def update_stacks(set_region, set_name, accts, regions, param_overrides,
                  ops_prefs):
    # Wrapper for update_stack_instances
    sleep_time = 15
    retries = 20
    this_try = 0

    logger.info("Updating stacks with op prefs %s" % ops_prefs)

    # UpdateStackInstance only allows stackSetName, not stackSetId,
    # so we need to truncate.
    (set_name, uid) = set_name.split(':')
    logger.debug("StackSetName: %s, Accounts: %s, Regions: %s, ParameterOverrides: %s" % (set_name, accts, regions, param_overrides))

    while True:
        try:
            client = boto3.client('cloudformation', region_name=set_region)
            response = client.update_stack_instances(
                StackSetName=set_name,
                Accounts=accts,
                Regions=regions,
                ParameterOverrides=param_overrides,
                OperationPreferences=ops_prefs,
                # OperationId='string'
            )
            return(response)
        except ClientError as e:
            if e.response['Error']['Code'] == 'OperationInProgressException':
                this_try += 1
                if this_try == retries:
                    return("Failed to update stacks after %s tries" % this_try)
                else:
                    logger.warning("Operation in progress for %s in %s. Sleeping for %i seconds." % (set_name, set_region, sleep_time))
                    sleep(sleep_time)
                    continue
            elif e.response['Error']['Code'] == 'StackSetNotFoundException':
                raise Exception("No StackSet matching %s found in %s. You must create before launching stacks." % (set_name, set_region))
            else:
                raise Exception("Unexpected error: %s" % e)


def delete_stacks(set_region, set_id, accts, regions, ops_prefs):
    # Wrapper for delete_stack_instances
    sleep_time = 15
    retries = 20
    this_try = 0

    logger.info("Deleting stacks with op prefs %s" % ops_prefs)
    logger.debug("StackSetName: %s, Accounts: %s, Regions: %s" % (set_id, accts, regions))

    while True:
        try:
            client = boto3.client('cloudformation', region_name=set_region)
            response = client.delete_stack_instances(
                StackSetName=set_id,
                Accounts=accts,
                Regions=regions,
                OperationPreferences=ops_prefs,
                RetainStacks=False,
                # OperationId='string'
            )
            return(response)
        except ClientError as e:
            if e.response['Error']['Code'] == 'OperationInProgressException':
                this_try += 1
                if this_try == retries:
                    return("Failed to delete stacks after %s tries" % this_try)
                else:
                    logger.warning("Operation in progress for %s in %s. Sleeping for %i seconds." % (set_id, set_region, sleep_time))
                    sleep(sleep_time)
                    continue
            elif e.response['Error']['Code'] == 'StackSetNotFoundException':
                return("No StackSet matching %s found in %s. You must create before launching stacks." % (set_id, set_region))
            else:
                return("Unexpected error: %s" % e)


def update_stack_set(set_region, set_id, set_description, set_template,
                     set_parameters, set_capabilities, set_tags, ops_prefs):
    # Set up for retries
    sleep_time = 15
    retries = 20
    this_try = 0

    client = boto3.client('cloudformation', region_name=set_region)

    # Retry loop
    while True:
        try:
            response = client.update_stack_set(
                StackSetName=set_id,
                Description=set_description,
                TemplateURL=set_template,
                # TemplateBody='string',
                # UsePreviousTemplate=True|False,
                Parameters=set_parameters,
                Capabilities=set_capabilities,
                Tags=set_tags,
                OperationPreferences=ops_prefs
                # OperationId='string'
            )
            if response['ResponseMetadata']['HTTPStatusCode'] == 200:
                return set_id
            else:
                raise Exception("HTTP Error: %s" % response)
        except ClientError as e:
            if e.response['Error']['Code'] == 'OperationInProgressException':
                this_try += 1
                if this_try == retries:
                    raise Exception("Failed to delete StackSet after %s tries." % this_try)
                else:
                    logger.warning("Operation in progress for %s. Sleeping for %i seconds." % (set_id, sleep_time))
                    sleep(sleep_time)
                    continue
            elif e.response['Error']['Code'] == 'StackSetNotEmptyException':
                raise Exception("There are still stacks in set %s. You must delete these first." % (set_id))
            else:
                raise Exception("Unexpected error: %s" % e)


def create(event, context):
    """
    Handle StackSetResource CREATE events.

    Create StackSet resource and any stack instances specified in the template.
    """
    # Collect everything we need to create the stack set

    # optional
    if 'StackSetName' in event['ResourceProperties']:
        set_name = event['ResourceProperties']['StackSetName']
    else:
        set_name = "%s-%s" % (get_stack_from_arn(event['StackId']), event['LogicalResourceId'])

    if 'StackSetDescription' in event['ResourceProperties']:
        set_description = event['ResourceProperties']['StackSetDescription']
    else:
        set_description = "This StackSet belongs to the CloudFormation stack %s." % get_stack_from_arn(event['StackId'])

    if 'OperationPreferences' in event['ResourceProperties']:
        set_opsprefs = convert_ops_prefs(event['ResourceProperties']['OperationPreferences'])
    else:
        set_opsprefs = {}

    if 'Tags' in event['ResourceProperties']:
        set_tags = expand_tags(event['ResourceProperties']['Tags'])
    else:
        set_tags = []

    if 'Capabilities' in event['ResourceProperties']:
        set_capabilities = event['ResourceProperties']['Capabilities']
    else:
        set_capabilities = ''

    if 'AdministrationRoleARN' in event['ResourceProperties']:
        set_admin_role_arn = event['ResourceProperties']['AdministrationRoleARN']
    else:
        set_admin_role_arn = ''
    
    if 'ExecutionRoleName' in event['ResourceProperties']:
        set_exec_role_name = event['ResourceProperties']['ExecutionRoleName']
    else:
        set_exec_role_name = ''

    if 'Parameters' in event['ResourceProperties']:
        set_parameters = expand_parameters(event['ResourceProperties']['Parameters'])
    else:
        set_parameters = []

    # Required
    set_template = event['ResourceProperties']['TemplateURL']
    

    # Create the StackSet
    try:
        client = boto3.client('cloudformation',
                              region_name=os.environ['AWS_REGION'])
        response = client.create_stack_set(
            StackSetName=set_name,
            Description=set_description,
            TemplateURL=set_template,
            # TemplateBody='string',
            Parameters=set_parameters,
            Capabilities=set_capabilities,
            Tags=set_tags,
            AdministrationRoleARN=set_admin_role_arn,
            ExecutionRoleName=set_exec_role_name
            # ClientRequestToken='string'
        )
        if response['ResponseMetadata']['HTTPStatusCode'] == 200:
            set_id = response['StackSetId']
        else:
            raise Exception("HTTP Error: %s" % response)
    except ClientError as e:
        if e.response['Error']['Code'] == 'NameAlreadyExistsException':
            raise Exception("A StackSet called %s already exists." % set_name)
        else:
            raise Exception("Unexpected error: %s" % e)
    logger.info("Created StackSet: %s" % set_id)
    physical_resource_id = set_id

    # Deploy stack to accounts and regions if defined.
    # We're going to switch from a single stack instance definition to an array
    # of stack instance objects.  This will allow more complex stack structures
    # across accounts and regions, including parameter overrides

    # Iterate over stack instances
    for instance in event['ResourceProperties']['StackInstances']:
        if 'ParameterOverrides' in instance:
            param_overrides = expand_parameters(instance['ParameterOverrides'])
        else:
            param_overrides = []
        logger.debug("Stack Instance: Regions: %s : Accounts: %s : Parameters: %s" % (instance['Regions'], instance['Accounts'], param_overrides))

        # Make sure every stack instance defines both a list of accounts and
        # a list of regions
        if instance['Regions'][0] != '' and instance['Accounts'][0] == '':
            raise Exception("You must specify at least one account with a list of regions.")
        elif instance['Regions'][0] == '' and instance['Accounts'][0] != '':
            raise Exception("You must specify at least one region with a list of accounts.")
        elif instance['Regions'][0] != '' and instance['Accounts'][0] != '':
            logger.info("Launching stacks in accounts: %s and regions: %s" % (instance['Accounts'], instance['Regions']))
            response = launch_stacks(
                    os.environ['AWS_REGION'],
                    set_id,
                    instance['Accounts'],
                    instance['Regions'],
                    param_overrides,
                    set_opsprefs
            )
            logger.debug(response)
    response_data = {}
    return physical_resource_id, response_data


def update(event, context):
    """
    Handle StackSetResource UPDATE events.

    Update StackSet resource and/or any stack instances specified in the template.
    """

    # Collect everything we need to update the stack set
    set_id = event['PhysicalResourceId']

    # Process the Operational Preferences (if any)
    if 'OperationPreferences' in event['ResourceProperties']:
        set_opsprefs = convert_ops_prefs(event['ResourceProperties']['OperationPreferences'])
    else:
        set_opsprefs = {}
    logger.debug("OperationPreferences: %s" % set_opsprefs)

    # Circumstances under which we update the StackSet itself
    stack_set_attributes = [
        'TemplateURL',
        'Parameters',
        'Tags',
        'Capabilities',
        'StackSetDecription'
    ]
    stack_set_needs_update = change_requires_update(stack_set_attributes,
                                                    event['OldResourceProperties'],
                                                    event['ResourceProperties'])

    if stack_set_needs_update:
        logger.info("Changes impacting StackSet detected")

        # Optional properties
        logger.info("Evaluating optional properties")
        if 'StackSetDescription' in event['ResourceProperties']:
            set_description = event['ResourceProperties']['StackSetDescription']
        elif 'StackSetDescription' in event['OldResourceProperties']:
            set_description = event['OldResourceProperties']['StackSetDescription']
        else:
            set_description = "This StackSet belongs to the CloudFormation stack %s." % get_stack_from_arn(event['StackId'])
        logger.debug("StackSetDescription: %s" % set_description)

        if 'Capabilities' in event['ResourceProperties']:
            set_capabilities = event['ResourceProperties']['Capabilities']
        elif 'Capabilities' in event['OldResourceProperties']:
            set_capabilities = event['OldResourceProperties']['Capabilities']
        else:
            set_capabilities = []
        logger.debug("Capabilities: %s" % set_capabilities)

        if 'Tags' in event['ResourceProperties']:
            set_tags = expand_tags(event['ResourceProperties']['Tags'])
        elif 'Tags' in event['OldResourceProperties']:
            set_tags = expand_tags(event['OldResourceProperties']['Tags'])
        else:
            set_tags = []
        logger.debug("Tags: %s" % set_tags)

        if 'Parameters' in event['ResourceProperties']:
            set_parameters = expand_parameters(event['ResourceProperties']['Parameters'])
        elif 'Parameters' in event['OldResourceProperties']:
            set_parameters = expand_parameters(event['OldResourceProperties']['Parameters'])
        else:
            set_parameters = []
        logger.debug("Parameters: %s" % set_parameters)

        # Required properties
        logger.info("Evaluating required properties")
        if 'TemplateURL' in event['ResourceProperties']:
            set_template = event['ResourceProperties']['TemplateURL']
        elif 'TemplateURL' in event['OldResourceProperties']:
            set_template = event['OldResourceProperties']['TemplateURL']
        else:
            raise Exception('Template URL not found during update event')
        logger.debug("TemplateURL: %s" % set_template)

        # Update the StackSet
        logger.info("Updating StackSet resource %s" % set_id)
        update_stack_set(os.environ['AWS_REGION'], set_id, set_description,
                         set_template, set_parameters, set_capabilities,
                         set_tags, set_opsprefs)

    # Now, look for changes to stack instances
    logger.info("Evaluating stack instances")

    # Flatten all the account/region tuples to compare differences
    if 'StackInstances' in event['ResourceProperties']:
        new_stacks = flatten_stacks(event['ResourceProperties']['StackInstances'])
    else:
        new_stacks = []

    if 'StackInstances' in event['OldResourceProperties']:
        old_stacks = flatten_stacks(event['OldResourceProperties']['StackInstances'])
    else:
        old_stacks = []

    # Evaluate all differences we need to handle
    to_add = list(set(new_stacks) - set(old_stacks))
    to_delete = list(set(old_stacks) - set(new_stacks))
    to_compare = list(set(old_stacks).intersection(new_stacks))

    # Launch all new stack instances
    if to_add:
        logger.info("Adding stack instances:  %s" % to_add)

        # Aggregate accounts with similar regions to reduce number of API calls
        add_instances = aggregate_instances(to_add, new_stacks)

        # Add stack instances
        for instance in add_instances:
            logger.debug("Add aggregated accounts: %s and regions: %s and overrides: %s" % (instance['accounts'], instance['regions'], instance['overrides']))
            if 'overrides' in instance:
                param_overrides = expand_parameters(instance['overrides'])
            else:
                param_overrides = []

            response = launch_stacks(
                    os.environ['AWS_REGION'],
                    set_id,
                    instance['accounts'],
                    instance['regions'],
                    param_overrides,
                    set_opsprefs
            )
            logger.debug(response)

    # Delete all old stack instances
    if to_delete:
        logger.info("Deleting stack instances: %s" % to_delete)

        # Aggregate accounts with similar regions to reduce number of API calls
        delete_instances = aggregate_instances(to_delete, old_stacks)

        # Add stack instances
        for instance in delete_instances:
            logger.debug("Delete aggregated accounts: %s and regions: %s" % (instance['accounts'], instance['regions']))
            response = delete_stacks(
                    os.environ['AWS_REGION'],
                    set_id,
                    instance['accounts'],
                    instance['regions'],
                    set_opsprefs
            )
            logger.debug(response)

    # Determine if any existing instances need to be updated
    if to_compare:
        logger.info("Examining stack instances: %s" % to_compare)

        # Update any stacks in both lists, but with new overrides
        to_update = []
        for instance in to_compare:
            if old_stacks[instance] == new_stacks[instance]:
                logger.debug("%s: SAME!" % instance)
            else:
                logger.debug("%s: DIFFERENT!" % instance)
                to_update.append(instance)

        # Aggregate accounts with similar regions to reduce number of API calls
        update_instances = aggregate_instances(to_update, new_stacks)
        for instance in update_instances:
            logger.debug("Update aggregated accounts: %s and regions: %s with overrides %s" % (instance['accounts'], instance['regions'], instance['overrides']))
            if 'overrides' in instance:
                param_overrides = expand_parameters(instance['overrides'])
            else:
                param_overrides = []

            response = update_stacks(
                    os.environ['AWS_REGION'],
                    set_id,
                    instance['accounts'],
                    instance['regions'],
                    param_overrides,
                    set_opsprefs
            )
            logger.debug(response)

    physical_resource_id = set_id
    response_data = {}
    return physical_resource_id, response_data


def delete(event, context):
    """
    Handle StackSetResource DELETE events.

    Delete StackSet resource and any stack instances specified in the template.
    """
    # Set up for retries
    sleep_time = 15
    retries = 20
    this_try = 0

    # Collect everything we need to delete the stack set
    set_id = event['PhysicalResourceId']

    if set_id == 'NONE':
        # This is a rollback from a failed create.  Nothing to do.
        return

    # First, we need to tear down all of the stacks associated with this
    # stack set
    if 'StackInstances' in event['ResourceProperties']:
        # Check for Operation Preferences
        if 'OperationPreferences' in event['ResourceProperties']:
            set_opsprefs = convert_ops_prefs(event['ResourceProperties']['OperationPreferences'])
        else:
            set_opsprefs = {}

        # Iterate over stack instances
        for instance in event['ResourceProperties']['StackInstances']:
            logger.debug("Stack Instance: Regions: %s : Accounts: %s" % (instance['Regions'], instance['Accounts']))

            logger.info("Removing existing stacks from stack set %s" % set_id)

            response = delete_stacks(
                os.environ['AWS_REGION'],
                set_id,
                instance['Accounts'],
                instance['Regions'],
                set_opsprefs
            )
            logger.debug(response)

    client = boto3.client('cloudformation',
                          region_name=os.environ['AWS_REGION'])

    # Retry loop
    logger.info('Deleting stack set')
    while True:
        try:
            response = client.delete_stack_set(
                StackSetName=set_id
            )
            if response['ResponseMetadata']['HTTPStatusCode'] == 200:
                return
            else:
                raise Exception("HTTP Error: %s" % response)
        except ClientError as e:
            if e.response['Error']['Code'] == 'OperationInProgressException':
                this_try += 1
                if this_try == retries:
                    raise Exception("Failed to delete StackSet after %s tries." % this_try)
                else:
                    logger.warning("Operation in progress for %s. Sleeping for %i seconds." % (set_id, sleep_time))
                    sleep(sleep_time)
                    continue
            elif e.response['Error']['Code'] == 'StackSetNotEmptyException':
                raise Exception("There are still stacks in set %s. You must delete these first." % (set_id))
            else:
                raise Exception("Unexpected error: %s" % e)


def handler(event, context):
    """
    Main handler function, passes off it's work to crhelper's cfn_handler
    """
    # update the logger with event info
    global logger
    logger = crhelper.log_config(event)
    return crhelper.cfn_handler(event, context, create, update, delete, logger,
                                init_failed)
