# Deploy CI infrastructure for the app, which will in turn deploy the app
# itself.
#
# Portions adapted from the AWS docs:
# http://docs.aws.amazon.com/codepipeline/latest/userguide/how-to-lambda-integration.html#LambdaSample1

# Thanks also to Boto; it's probably my favorite of the AWS SDKs.
from __future__ import print_function

import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import time
import urllib2
import zipfile

import boto3
import botocore

def update_stack(cf, stack, template, stack_params):
    """Start a CloudFormation stack update

    Args:
        cf: A Boto3 CloudFormation client
        stack: The stack to update
        template: Text of the template to use.
        stack_params: Parameters for the stack.

    Returns:
        True if an update was started, False if there were no changes
        to the template since the last update.

    Raises:
        Exception: Any exception besides "No updates are to be performed."

    """
    try:
        print("Updating stack " + stack)
        cf.update_stack(StackName=stack, TemplateBody=template,
                        Parameters=stack_params,
                        Capabilities=['CAPABILITY_IAM'])
        return True

    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Message'] == 'No updates are to be performed.':
            return False
        else:
            raise Exception('Error updating CloudFormation stack "{0}"'.format(stack), e)

def stack_exists(cf, stack):
    """Check if a stack exists or not

    Args:
        cf: A Boto3 CloudFormation client
        stack: The stack to check

    Returns:
        True or False depending on whether the stack exists

    Raises:
        Any exceptions raised .describe_stacks() besides that
        the stack doesn't exist.

    """
    try:
        cf.describe_stacks(StackName=stack)
        return True
    except botocore.exceptions.ClientError as e:
        if "does not exist" in e.response['Error']['Message']:
            return False
        else:
            raise e

def create_stack(cf, stack, template, stack_params):
    """Starts a new CloudFormation stack creation

    Args:
        cf: A Boto3 CloudFormation client
        stack: The stack to be created
        template: The template for the stack to be created with
        stack_params: Parameters for the stack.

    Throws:
        Exception: Any exception thrown by .create_stack()
    """
    print("Creating stack " + stack)
    cf.create_stack(StackName=stack, TemplateBody=template, Parameters=stack_params,
                    Capabilities=['CAPABILITY_IAM'])

def get_stack_info(cf, stack):
    description = cf.describe_stacks(StackName=stack)
    return description['Stacks'][0]

def get_stack_status(cf, stack):
    """Get the status of an existing CloudFormation stack

    Args:
        cf: A Boto3 CloudFormation client
        stack: The name of the stack to check

    Returns:
        The CloudFormation status string of the stack such as CREATE_COMPLETE

    Raises:
        Exception: Any exception thrown by .describe_stacks()

    """
    info = get_stack_info(cf, stack)
    return info['StackStatus']

def get_stack_outputs(cf, stack):
    info = get_stack_info(cf, stack)
    return { o['OutputKey']: o['OutputValue'] for o in info['Outputs'] }

def aws_region():
    return botocore.session.Session().get_config_variable('region')

def aws_account_id():
    try:
        return boto3.client('iam').get_user()['User']['Arn'].split(':')[4]
    except:
        print("Unable to retrieve AWS account ID")
        sys.exit(1)

def wait_for_stack_success(cf, stack_name, pretty_name, verb, timeout=600):
    """Monitor a running CloudFormation update/create and return the final state.

    Args:
        cf: A Boto3 CloudFormation client
        stack_name: The stack to monitor
        pretty_name: Name of the stack (e.g. "ci", "web")
        verb: Pretty name of the action ('create' or 'update') for logs
        timeout: How many seconds to wait before timing out.

    Returns:
        True if the stack create/update succeeded, False if it failed or timed out
    """
    start_time = time.time()
    while True:
        status = get_stack_status(cf, stack_name)
        if status in ['UPDATE_COMPLETE', 'CREATE_COMPLETE']:
            print(pretty_name + " stack successfully " + verb +'d')
            return True

        elif status in ['UPDATE_IN_PROGRESS', 'UPDATE_ROLLBACK_IN_PROGRESS',
                        'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS',
                        'CREATE_IN_PROGRESS', 'ROLLBACK_IN_PROGRESS']:
            if time.time() - start_time > timeout:
                print("Timed out waiting for " + pretty_name + " stack " + verb)
                return False
        else:
            print(pretty_name + " stack " + verb + " failed: current status " + status)
            return False
        time.sleep(15)

def wait_for_stack_existence(cf, stack_name, timeout=600):
    start_time = time.time()
    print("Waiting for stack " + stack_name + " to appear...")
    while not stack_exists(cf, stack_name):
        if time.time() - start_time > timeout:
            print("Timed out waiting for stack " + stack_name + " to appear")
            return False
        time.sleep(15)
    return True

def ensure_bucket(s3, bucket_name):
    try:
        s3.head_bucket(Bucket=bucket_name)
    except:
        print("Creating bucket " + bucket_name + " to hold builds")
        # Work around a truly infuriating inconsistency in the AWS API:
        # you can't use a LocationConstraint of us-east-1 or it fails.
        reg = aws_region()
        if reg != 'us-east-1':
            s3.create_bucket(Bucket=bucket_name,
                             CreateBucketConfiguration={'LocationConstraint': reg})
        else:
            s3.create_bucket(Bucket=bucket_name)
        boto3.resource('s3').Bucket(bucket_name).wait_until_exists()
    versioning = s3.get_bucket_versioning(Bucket=bucket_name)
    try:
        versioning_enabled = (versioning['Status'] == 'Enabled')
    except KeyError:
        versioning_enabled = False
    if not versioning_enabled:
        s3.put_bucket_versioning(Bucket=bucket_name,
                                 VersioningConfiguration={'Status': 'Enabled'})
    return

def upload_lambda_functions(s3, bucket_name, object_key):
    """Zip up lambda functions and upload to S3.

    Args:
       s3: A Boto3 S3 client
       bucket_name: Name of the bucket
       object_key: Name of the object where lambdas should be put

    Returns:
       The VersionId of the object that was written.
    """
    thisdir = os.path.dirname(os.path.abspath(__file__))
    lambda_path = os.path.join(thisdir, '..', 'lambda')
    with tempfile.NamedTemporaryFile() as tmp_file:
        zip_name = shutil.make_archive(tmp_file.name, 'zip', lambda_path)
        s3.upload_file(zip_name, bucket_name, object_key)
        # Immediately fetch the head of the object back to get latest version
        obj = s3.head_object(Bucket=bucket_name, Key=object_key)
        return obj['VersionId']

def test_web_site(siteURL):
    """Run acceptance tests on the given web site.

    Args:
        siteURL: URL of the website

    Returns:
        True if the site is returning reasonable output on port 80.
    """
    try:
        body = urllib2.urlopen(siteURL, None, 5).read()
        # We just search directly for the phrase
        # TODO: Abstract this into a suite of separate tests
        return (re.search('Automation for the People', body) != None)
    except urllib2.URLError:
        return False

def assert_config():
    """Print an error and exit if required configuration is missing.
    """
    if aws_region is None:
        print("you must configure the AWS region (perhaps via the AWS_DEFAULT_REGION env var) before running this script")
        sys.exit(1)
    if os.getenv('AWS_EC2_KEYNAME') == None:
        print("you must set the AWS_EC2_KEYNAME env var before running this script")
        sys.exit(1)
    if os.getenv('GITHUB_USERNAME') == None:
        print("you must set the GITHUB_USERNAME env var before running this script")
        sys.exit(1)
    if os.getenv('GITHUB_OAUTH_TOKEN') == None:
        print("you must set the GITHUB_OAUTH_TOKEN env var before running this script")
        sys.exit(1)

def assemble_ci_stack_parameters(app_name, bucket_name, lambda_key, lambda_version):
    """Sets up CI stack parameters from environment variables.

    Args:
        app_name: The app name to use. This will become a parameter.

    Returns:
        A Parameters option for update_stack or create_stack: An array of dicts.

    Raises:
        Exception: If a required stack parameter is missing.
    """
    assert_config()
    stack_params = [{
                        'ParameterKey': 'AppName',
                        'ParameterValue': app_name
                    },{
                        'ParameterKey': 'BuildBucket',
                        'ParameterValue': bucket_name
                    },{
                        'ParameterKey': 'LambdaKey',
                        'ParameterValue': lambda_key
                    },{
                        'ParameterKey': 'LambdaLatestVersion',
                        'ParameterValue': lambda_version
                    },{
                        'ParameterKey': 'GitHubUser',
                        'ParameterValue': os.getenv('GITHUB_USERNAME')
                    },{
                        'ParameterKey': 'GitHubToken',
                        'ParameterValue': os.getenv('GITHUB_OAUTH_TOKEN')
                    },{
                        'ParameterKey': 'GitHubRepoName',
                        'ParameterValue': os.getenv('GITHUB_REPO_NAME', 'aws-ci-demo')
                    },{
                        'ParameterKey': 'GitHubBranchName',
                        'ParameterValue': os.getenv('GITHUB_BRANCH_NAME', 'master')
                    },{
                        'ParameterKey': 'WebStackName',
                        'ParameterValue': os.getenv('WEB_STACK_NAME', app_name + '-web')
                    },{
                        'ParameterKey': 'KeyName',
                        'ParameterValue': os.getenv('AWS_EC2_KEYNAME')
                    }]
    return stack_params

if __name__ == "__main__":
    try:
        thisdir = os.path.dirname(os.path.abspath(__file__))
        app_name = os.getenv('APP_NAME', 'a4tp')
        assert_config()

        s3 = boto3.client('s3')
        cf = boto3.client('cloudformation')

        # Launching is a three-stage operation:
        #
        #  - Ensure the S3 bucket exists and upload lambda functions to it.
        #  - The CI stack is launched and creates the CI pipeline.
        #  - The CI pipeline creates the web stack, which runs the app and
        #    outputs the load balancer DNS name, which we use to access the app.

        bucket_name = 'builds-' + app_name + '-' + aws_region() + '-' + aws_account_id()
        print("Ensuring that build bucket exists: " + bucket_name)
        ensure_bucket(s3, bucket_name)

        lambda_key = 'Lambdas.zip'
        print("Zipping and uploading lambda functions to s3://" + bucket_name + '/' + lambda_key)
        lambda_version = upload_lambda_functions(s3, bucket_name, lambda_key)

        with open(os.path.join(thisdir, '../cfn/ci.template'), 'r') as f:
            ci_template = f.read()
        ci_params = assemble_ci_stack_parameters(app_name, bucket_name,
                                                 lambda_key, lambda_version)
        ci_stack_name = os.getenv('CI_STACK_NAME', app_name + "-ci")
        if stack_exists(cf, ci_stack_name):
            print("Found the CI stack " + ci_stack_name)
            status = get_stack_status(cf, ci_stack_name)
            if status not in ['CREATE_COMPLETE', 'ROLLBACK_COMPLETE', 'UPDATE_COMPLETE']:
                print('CI stack cannot be updated when status is: ' + status)
                sys.exit(1)

            were_updates = update_stack(cf, ci_stack_name, ci_template, ci_params)
            if were_updates:
                if not wait_for_stack_success(cf, ci_stack_name, 'CI', 'update'):
                    sys.exit(1)
        else:
            # If the stack doesn't already exist then create it instead
            # of updating it.
            create_stack(cf, ci_stack_name, ci_template, ci_params)
            if not wait_for_stack_success(cf, ci_stack_name, 'CI', 'create'):
                sys.exit(1)

        ci_outputs = get_stack_outputs(cf, ci_stack_name)
        print("Deploying code from " + ci_outputs['ApplicationSource'])
        print("Visit " + ci_outputs['CodePipelineURL'] + ' to view pipeline state')

        web_stack_name = ci_outputs['WebStackName']
        if not wait_for_stack_existence(cf, web_stack_name):
            sys.exit(1)
        if not wait_for_stack_success(cf, web_stack_name, 'web', 'deploy'):
            sys.exit(1)
        web_outputs = get_stack_outputs(cf, web_stack_name)
        print("The deployed build is " + web_outputs['ApplicationBuild'])

        siteURL = 'http://' + web_outputs['BalancerDNSName']
        print("Visit your website at " + siteURL)

        if not test_web_site(siteURL):
            print("The AutoScaling group may still be launching. Waiting for the balancer to return the expected web page...")
            print("Querying " + siteURL)
            start_time = time.time()
            while not test_web_site(siteURL):
                if time.time() - start_time > 600:
                    print("Waited for five minutes without success.")
                    sys.exit(1)
                sleep(10)

    except Exception as e:
        # If any other exceptions which we didn't expect are raised
        # then fail the job and log the exception message.
        print('Function failed due to exception.')
        print(e)
        traceback.print_exc()

