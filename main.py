from fastapi import FastAPI, HTTPException, Request
import json
import utils.aws_web_acl
import traceback
import glueops.checksum_tools
import glueops.setup_logging
import os
import asyncio

logger = glueops.setup_logging.configure(level=os.environ.get('LOG_LEVEL', 'WARNING'))

app = FastAPI()


@app.post("/sync")
async def post_sync(request: Request):
    try:
        data = await request.json()
        parent = data["parent"]
        children = data["children"]
        return await asyncio.to_thread(sync, parent, children)
    except Exception as e:
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=traceback.format_exc())


@app.post("/finalize")
async def post_finalize(request: Request):
    return {"finalized": True}


def sync(parent, children):
    status_dict = {}
    try:
        name, aws_resource_tags, web_acl_definition, status_dict, web_acl_arn, checksum_updated, web_acl_definition_hash = get_parent_data(
            parent)

        if web_acl_arn is None:
            acl_config = utils.aws_web_acl.generate_web_acl_configuration(
                web_acl_definition, aws_resource_tags)
            status_dict["web_acl_request"] = utils.aws_web_acl.create_web_acl(
                acl_config)
        elif checksum_updated:
            logger.info("Updating existing web_acl_arn")
            acl_config = utils.aws_web_acl.generate_web_acl_configuration(
                web_acl_definition, aws_resource_tags)
            status_dict["web_acl_request"] = utils.aws_web_acl.get_existing_web_acl(
                acl_config)
            acl_config = utils.aws_web_acl.generate_web_acl_configuration(
                web_acl_definition, aws_resource_tags, lock_token=status_dict["web_acl_request"]["LockToken"])
            utils.aws_web_acl.update_web_acl(acl_config, web_acl_arn)
            status_dict["web_acl_request"] = utils.aws_web_acl.get_current_state_of_web_acl_arn(
                web_acl_arn)
        elif not checksum_updated:
            logger.info(f"No Updates to be made for {web_acl_arn}")

        if "error_message" in status_dict:
            logger.info("Deleting old error_message")
            del status_dict["error_message"]

        status_dict["CRC32_HASH"] = web_acl_definition_hash
        status_dict["HEALTHY"] = "True"
        return {"status": status_dict}
    except Exception as e:
        status_dict["error_message"] = traceback.format_exc()
        status_dict["HEALTHY"] = "False"
        logger.error(status_dict["error_message"])
        return {"status": status_dict}


def finalize_hook(aws_resource_tags):
    try:
        arns = glueops.aws.get_resource_arns_using_tags(
            aws_resource_tags, ["wafv2"])
        if len(arns) > 1:
            raise Exception(
                "Multiple WebACL's with the same tags. Manual cleanup is required.")
        elif len(arns) == 1:
            utils.aws_web_acl.delete_web_acl(arns[0])
        return {"finalized": True}
    except Exception as e:
        logger.error(f"Unexpected exception occurred: {e}")
        return {"finalized": False, "error": str(e)}


def get_parent_data(parent):
    name = parent["metadata"].get("name")
    captain_domain = os.environ.get('CAPTAIN_DOMAIN')
    aws_resource_tags = [
        {"Key": "kubernetes_resource_name", "Value": name},
        {"Key": "captain_domain", "Value": captain_domain}
    ]
    web_acl_definition = parent.get("spec", {}).get("web_acl_definition")
    if web_acl_definition:
        web_acl_definition = json.loads(web_acl_definition)
        web_acl_definition["Name"] = parent["metadata"].get("name")
        web_acl_definition_hash = glueops.checksum_tools.string_to_crc32(
            json.dumps(web_acl_definition))

    status_dict = parent.get("status", {})
    status_dict["HEALTHY"] = "False"
    web_acl_arn = status_dict.get("web_acl_request", {}).get("ARN", None)
    checksum_updated = False
    if status_dict.get("CRC32_HASH"):
        if status_dict["CRC32_HASH"] != web_acl_definition_hash:
            checksum_updated = True
    if not utils.aws_web_acl.does_web_acl_exist(web_acl_arn):
        web_acl_arn = None
    return name, aws_resource_tags, web_acl_definition, status_dict, web_acl_arn, checksum_updated, web_acl_definition_hash
