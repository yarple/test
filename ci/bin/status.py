# Check the status of the deployed application.
from __future__ import print_function

import boto3
from provision import *

if __name__ == "__main__":
    cf = boto3.client('cloudformation')

    app_name = os.getenv('APP_NAME', 'a4tp')
    ci_stack_name = os.getenv('CI_STACK_NAME', app_name + "-ci")
    if stack_exists(cf, ci_stack_name):
        ci_outputs = get_stack_outputs(cf, ci_stack_name)
        try:
            web_stack = ci_outputs['WebStackName']
        except KeyError:
            web_stack = None
        if web_stack is None or not stack_exists(cf, web_stack):
            print('Web stack not found')
            sys.exit(1)
        web_outputs = get_stack_outputs(cf, web_stack)
        if test_web_site('http://' + web_outputs['BalancerDNSName']):
            print("OK")
            sys.exit(0)
        else:
            print("NOT OK")
            sys.exit(1)
    else:
        print("CI stack not found")
        sys.exit(1)
