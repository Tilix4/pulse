import project_config as cfg

def string_to_dict(uri_string):
    """
    transform a string uri in a dict uri
    :param uri_string:
    :return uri dict:
    """
    uri_split_main = uri_string.split("@")
    uri_split = uri_split_main[0].split("-")
    entity = uri_split[0]
    resource_type = uri_split[1]

    if len(uri_split_main)>1:
        version = uri_split_main[1]
        return {"entity": entity, "resource_type": resource_type, "version": version}
    else:
        return {"entity": entity, "resource_type": resource_type}


def dict_to_string(uri_dict):
    """
    :param uri_string:
    :return uri dict:
    """
    uri = uri_dict["entity"] + "-" + uri_dict['resource_type']
    if 'version' in uri_dict:
        uri += "@" + str(uri_dict['version'].zfill(cfg.VERSION_PADDING))
    return uri
