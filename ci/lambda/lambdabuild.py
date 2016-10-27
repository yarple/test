# Perform a build of the site source code, as a step in a CodePipeline
#
# Code derived from:
# https://stelligent.com/2016/02/15/mocking-aws-codepipeline-pipelines-with-lambda/
# http://docs.aws.amazon.com/codepipeline/latest/userguide/how-to-lambda-integration.html#LambdaSample1
from __future__ import print_function
from boto3.session import Session

import json
import urllib
import boto3
import zipfile
import tempfile
import botocore
import traceback
import os, errno, shutil

code_pipeline = boto3.client('codepipeline')

def find_artifact(artifacts, name):
    """Finds the artifact 'name' among the 'artifacts'

    Args:
        artifacts: The list of artifacts available to the function
        name: The artifact we wish to use
    Returns:
        The artifact dictionary found
    Raises:
        Exception: If no matching artifact is found

    """
    for artifact in artifacts:
        if artifact['name'] == name:
            return artifact

    raise Exception('Input artifact named "{0}" not found in event'.format(name))

def perform_build(src_dir, dest_dir):
    # copy all items from src_dir to dest_dir
    for f in os.listdir(src_dir):
        try:
            shutil.copytree(os.path.join(src_dir, f), os.path.join(dest_dir, f))
        except OSError as exc:
            if exc.errno == errno.ENOTDIR:
                shutil.copy(os.path.join(src_dir, f), os.path.join(dest_dir, f))
            else: raise

def put_job_success(job, message):
    """Notify CodePipeline of a successful job

    Args:
        job: The CodePipeline job ID
        message: A message to be logged relating to the job status

    Raises:
        Exception: Any exception thrown by .put_job_success_result()

    """
    print('Putting job success')
    print(message)
    code_pipeline.put_job_success_result(jobId=job)

def put_job_failure(job, message):
    """Notify CodePipeline of a failed job

    Args:
        job: The CodePipeline job ID
        message: A message to be logged relating to the job status

    Raises:
        Exception: Any exception thrown by .put_job_failure_result()

    """
    print('Putting job failure')
    print(message)
    code_pipeline.put_job_failure_result(jobId=job, failureDetails={'message': message, 'type': 'JobFailed'})

def get_user_params(job_data):
    """Decodes the JSON user parameters and validates the required properties.

    Args:
        job_data: The job data structure containing the UserParameters string which should be a valid JSON structure

    Returns:
        The JSON parameters decoded as a dictionary.

    Raises:
        Exception: The JSON can't be decoded or a property is missing.

    """
    try:
        # Get the user parameters which contain the stack, artifact and file settings
        user_parameters = job_data['actionConfiguration']['configuration']['UserParameters']
        decoded_parameters = json.loads(user_parameters)

    except Exception as e:
        # We're expecting the user parameters to be encoded as JSON
        # so we can pass multiple values. If the JSON can't be decoded
        # then fail the job with a helpful message.
        raise Exception('UserParameters could not be decoded as JSON')

    required_items = ['source_artifact', 'build_artifact', 'template_artifact', 'template_subdir_path']
    for i in required_items:
        if i not in decoded_parameters:
            raise Exception('Your UserParameters JSON must include ' + i)
        print(i + " = " + decoded_parameters[i])

    return decoded_parameters

def get_zipped_artifact(s3, artifact):
    """Download source code from S3 and unzip to a temporary directory.

    Args:
        artifact: The artifact to download.

    Returns:
        Path of the temporary directory containing the artifact code. The
        caller is responsible for deleting this directory.

    Raises:
        Exception: Any exception thrown while downloading the artifact or unzipping it
    """
    tmp_dir = tempfile.mkdtemp()
    bucket = artifact['location']['s3Location']['bucketName']
    key = artifact['location']['s3Location']['objectKey']
    with tempfile.NamedTemporaryFile() as tmp_file:
        s3.download_file(bucket, key, tmp_file.name)
        with zipfile.ZipFile(tmp_file.name, 'r') as zip:
            zip.extractall(tmp_dir)
    return tmp_dir

def put_zipped_artifact(s3, src_dir, artifact):
    """Zip up the contents of a directory and upload to an S3 artifact.

    Args:
        src_dir: The directory to zip.
        artifact: The artifact to upload the zipfile to.

    Raises:
        Exception: Any exception thrown while downloading the artifact or unzipping it
    """
    bucket = artifact['location']['s3Location']['bucketName']
    key = artifact['location']['s3Location']['objectKey']
    with tempfile.NamedTemporaryFile() as tmp_file:
        zip_name = shutil.make_archive(tmp_file.name, 'zip', src_dir)
        s3.upload_file(zip_name, bucket, key)

def setup_s3_client(job_data):
    """Creates an S3 client

    Uses the credentials passed in the event by CodePipeline. These
    credentials can be used to access the artifact bucket.

    Args:
        job_data: The job data structure

    Returns:
        An S3 client with the appropriate credentials

    """
    key_id = job_data['artifactCredentials']['accessKeyId']
    key_secret = job_data['artifactCredentials']['secretAccessKey']
    session_token = job_data['artifactCredentials']['sessionToken']

    session = Session(aws_access_key_id=key_id,
        aws_secret_access_key=key_secret,
        aws_session_token=session_token)
    return session.client('s3', config=botocore.client.Config(signature_version='s3v4'))

def lambda_handler(event, context):
    """The Lambda function handler

    Perform a build of the source and export the built artifacts to
    future CodePipeline stages.

    Args:
        event: The event passed by Lambda
        context: The context passed by Lambda
"""
    try:
        # Extract the Job ID
        job_id = event['CodePipeline.job']['id']

        # Extract the Job Data
        job_data = event['CodePipeline.job']['data']

        # Extract the params
        params = get_user_params(job_data)

        # Get the lists of artifacts coming in and out of this function
        input_artifacts = job_data['inputArtifacts']
        output_artifacts = job_data['outputArtifacts']

        # Perform a build on the source (from source_artifact)
        # and write results to the build_artifact
        s3 = setup_s3_client(job_data)
        source_artifact = find_artifact(input_artifacts, params['source_artifact'])
        src_dir = get_zipped_artifact(s3, source_artifact)
        dest_dir = tempfile.mkdtemp()
        perform_build(os.path.join(src_dir, 'src'), dest_dir)
        build_artifact = find_artifact(output_artifacts, params['build_artifact'])
        put_zipped_artifact(s3, dest_dir, build_artifact)

        # Pick the template out of the source code and write it to the
        # template_artifact
        template_artifact = find_artifact(output_artifacts, params['template_artifact'])
        put_zipped_artifact(s3, os.path.join(src_dir, params['template_subdir_path']), template_artifact)

        shutil.rmtree(src_dir)
        shutil.rmtree(dest_dir)
        put_job_success(job_id, "Built code: " + ", template:")

    except Exception as e:
        # If any other exceptions which we didn't expect are raised
        # then fail the job and log the exception message.
        print('Function failed due to exception.')
        print(e)
        traceback.print_exc()
        put_job_failure(job_id, 'Function exception: ' + str(e))

    print('Function complete.')
    return "Complete."
