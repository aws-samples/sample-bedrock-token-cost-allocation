"""
Test Bedrock invocation script.
Sends 3 test requests through the Application Inference Profile to generate
model invocation logs for end-to-end pipeline testing.

Usage:
    python glue-etl/test_bedrock_invocation.py \
        --profile awssteph+sandbox-RootAccountAdmin \
        --region us-east-1 \
        --stack-name bedrock-logging
"""

import argparse
import json
import boto3


def get_inference_profile_arn(profile_name, region, stack_name):
    session = boto3.Session(profile_name=profile_name, region_name=region)
    cfn = session.client("cloudformation")
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    for o in outputs:
        if o["OutputKey"] == "BedrockInferenceProfileArn":
            return o["OutputValue"]
    raise ValueError(f"BedrockInferenceProfileArn not found in stack {stack_name}")


def invoke(client, model_id, i):
    body = json.dumps({
        "messages": [{"role": "user", "content": [{"text": f"Test invocation {i}"}]}],
        "inferenceConfig": {"max_new_tokens": 10},
    }).encode()
    resp = client.invoke_model(
        modelId=model_id,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(resp["body"].read())
    return result["output"]["message"]["content"][0]["text"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-name", default="bedrock-logging")
    parser.add_argument("--model-id", default=None,
                        help="Override model ID (default: reads from stack output)")
    args = parser.parse_args()

    if args.model_id:
        model_id = args.model_id
    else:
        print(f"Looking up inference profile ARN from stack '{args.stack_name}'...")
        model_id = get_inference_profile_arn(args.profile, args.region, args.stack_name)
        print(f"Using model: {model_id}")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("bedrock-runtime")

    for i in range(1, 4):
        print(f"\n--- Invocation {i} ---")
        text = invoke(client, model_id, i)
        print(text)

    print("\nDone. Check bedrock-logs-{account}-{region} for JSON.GZ log files.")


if __name__ == "__main__":
    main()
