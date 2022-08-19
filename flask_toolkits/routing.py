import enum
import json
import os
import inspect
import re
import typing as t
import pydantic
from collections import defaultdict
from flask import Flask, Blueprint, Response, jsonify, request, Request
from flask.scaffold import _sentinel
from functools import wraps
from typing import Any, Callable, Dict, Mapping, List, Tuple, Union, Optional
from pydantic import BaseModel, create_model
from werkzeug.datastructures import FileStorage

from .responses import JSONResponse
from .exceptions import SwaggerPathError
from .dependencies import Depends
from .schemas import BaseSchema
from .security import HTTPSecurityBase
from .params import (
    _ParamsClasses,
    ParamsType,
    _BodyClasses,
    _FormClasses,
    FormType,
    ParamSignature,
    Header,
    Path,
    Query,
    Body,
    Form,
    FormURLEncoded,
    File
)


class EndpointDefinition():
    """Define endpoint's properties that will be generated by `AutoSwagger`

    :param rule: endpoint path
    :param method: HTTP method [`GET`,`POST`, `PUT`, `DELETE`, `PATCH`]
    :param paired_params: paired argument key - http parameters [`PATH`, `HEADER`, `QUERY`, `BODY`]
    :param tags: endpoint's swagger tags
    :param summary: endpoint's swagger summary
    :param description: endpoint's swagger description
    :param response_description: endpoint's swagger response_description
    :param responses: endpoint's swagger responses
    :param auto_swagger: set this `True` will generate the endpoint swagger automatically using `AutoSwagger`
    :param custom_swagger: put your custom swagger definition
        - this variable will replace the `AutoSwagger` definition only
        for this endpoin
        - this will also removing swagger `tags` so you have to define the tags
        in it
        - example format :
            {
                "tags":[],
                "summary": "My endpoint,
                "parameters": [],
                "responses": {}
            }
    :param pydantic_model: 
    """
    _all_endpoints = []

    def __init__(
        self,
        rule: str,
        method: str,
        paired_params: Dict[str, ParamsType],
        tags: Optional[List[str]] = None,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        pydantic_model: BaseModel = None,
        security: Optional[HTTPSecurityBase] = None,
        aliases: Optional[Dict[str, Dict[str, str]]] = []
    ) -> None:
        self.rule = rule
        self.method = method.lower()
        self.paired_params = paired_params
        self.tags = tags
        self.summary = summary
        self.description = description
        self.response_description = response_description
        self.auto_swagger = auto_swagger
        self.custom_swagger = custom_swagger
        self.pydantic_model = pydantic_model
        self.security = security
        self.aliases = aliases
        if responses:
            self.responses = responses
        else:
            self.responses = {
                "200": {
                        "description": "Successful Response",
                        "content": {
                            "application/json": {
                                "schema": {}
                            }
                        }
                    },
                "422":{
                        "description": "ValidationError",
                        "content": {
                            "application/json": {
                                "example": {
                                    "detail": [
                                        {
                                        "loc": [
                                            "string"
                                        ],
                                        "msg": "string",
                                        "type": "string"
                                        }
                                    ]
                                }
                            }
                        }
                    }
            }
        EndpointDefinition._all_endpoints.append(self)


class APIRouter(Blueprint):
    """A subclass of `flask.Blueprint`.
    Any objects of this class will be registered as a router

    Use this class to make your router automatically documented by `AutoSwagger`

    :param name: The name of the blueprin Will be prepended to each
        endpoint name.
    :param import_name: The name of the blueprint package, usually
        ``__name__``. This helps locate the ``root_path`` for the
        blueprin
    :param static_folder: A folder with static files that should be
        served by the blueprint's static route. The path is relative to
        the blueprint's root path. Blueprint static files are disabled
        by defaul
    :param static_url_path: The url to serve static files from.
        Defaults to ``static_folder``. If the blueprint does not have
        a ``url_prefix``, the app's static route will take precedence,
        and the blueprint's static files won't be accessible.
    :param template_folder: A folder with templates that should be added
        to the app's template search path. The path is relative to the
        blueprint's root path. Blueprint templates are disabled by
        defaul Blueprint templates have a lower precedence than those
        in the app's templates folder.
    :param url_prefix: A path to prepend to all of the blueprint's URLs,
        to make them distinct from the rest of the app's routes.
    :param subdomain: A subdomain that blueprint routes will match on by
        defaul
    :param url_defaults: A dict of default values that blueprint routes
        will receive by defaul
    :param root_path: By default, the blueprint will automatically set
        this based on ``import_name``. In certain situations this
        automatic detection can fail, so the path can be specified
        manually instead.
    :param tags: endpoint's swagger tags
    :param auto_swagger: set this `True` will generate the endpoint 
        swagger automatically using `AutoSwagger`
    """

    _api_routers: Dict[str, Any] = {}

    def __init__(
        self,
        name: str,
        import_name: str,
        static_folder: Optional[Union[str, os.PathLike]] = None,
        static_url_path: Optional[str] = None,
        template_folder: Optional[str] = None,
        url_prefix: Optional[str] = "",
        subdomain: Optional[str] = None,
        url_defaults: Optional[dict] = None,
        root_path: Optional[str] = None,
        cli_group: Optional[str] = _sentinel,
        tags: Optional[List[str]] = [],
        auto_swagger: bool = True,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = []
    ):
        super().__init__(
            name=name,
            import_name=import_name,
            static_folder=static_folder,
            static_url_path=static_url_path,
            template_folder=template_folder,
            url_prefix=url_prefix,
            subdomain=subdomain,
            url_defaults=url_defaults,
            root_path=root_path,
            cli_group=cli_group
        )
        self.paired_signature: Dict[str, Dict[str, ParamsType]] = {}
        self.aliases: Dict[str, Dict[str, str]] = {}
        APIRouter._api_routers[name] = self
        self.defined_endpoints: List[EndpointDefinition] = []
        self._is_registered = False
        self._enable_auto_swagger = auto_swagger
        self.tags = tags
        self.security = security
        self.dependecies = dependencies
        self.available_methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]

    def register(self, app: Flask, options: dict) -> None:
        name_prefix = options.get("name_prefix", "")
        self_name = options.get("name", self.name)
        name = f"{name_prefix}.{self_name}".lstrip(".")

        if name in app.blueprints:
            existing_at = f" '{name}'" if self_name != name else ""

            if app.blueprints[name] is not self:
                raise ValueError(
                    f"The name '{self_name}' is already registered for"
                    f" a different blueprint{existing_at}. Use 'name='"
                    " to provide a unique name."
                )
            else:
                import warnings

                warnings.warn(
                    f"The name '{self_name}' is already registered for"
                    f" this blueprint{existing_at}. Use 'name=' to"
                    " provide a unique name. This will become an error"
                    " in Flask 2.1.",
                    stacklevel=4,
                )

        first_bp_registration = not any(bp is self for bp in app.blueprints.values())
        first_name_registration = name not in app.blueprints

        app.blueprints[name] = self
        self._got_registered_once = True
        self._is_registered = True
        state = self.make_setup_state(app, options, first_bp_registration)

        if self.has_static_folder:
            state.add_url_rule(
                f"{self.static_url_path}/<path:filename>",
                view_func=self.send_static_file,
                endpoint="static",
            )

        # Merge blueprint data into paren
        if first_bp_registration or first_name_registration:

            def extend(bp_dict, parent_dict):
                for key, values in bp_dict.items():
                    key = name if key is None else f"{name}.{key}"
                    parent_dict[key].extend(values)

            for key, value in self.error_handler_spec.items():
                key = name if key is None else f"{name}.{key}"
                value = defaultdict(
                    dict,
                    {
                        code: {
                            exc_class: func for exc_class, func in code_values.items()
                        }
                        for code, code_values in value.items()
                    },
                )
                app.error_handler_spec[key] = value

            for endpoint, func in self.view_functions.items():
                app.view_functions[endpoint] = func

            extend(self.before_request_funcs, app.before_request_funcs)
            extend(self.after_request_funcs, app.after_request_funcs)
            extend(
                self.teardown_request_funcs,
                app.teardown_request_funcs,
            )
            extend(self.url_default_functions, app.url_default_functions)
            extend(self.url_value_preprocessors, app.url_value_preprocessors)
            extend(self.template_context_processors, app.template_context_processors)

        for deferred in self.deferred_functions:
            deferred(state)

        cli_resolved_group = options.get("cli_group", self.cli_group)

        if self.cli.commands:
            if cli_resolved_group is None:
                app.cli.commands.update(self.cli.commands)
            elif cli_resolved_group is _sentinel:
                self.cli.name = name
                app.cli.add_command(self.cli)
            else:
                self.cli.name = cli_resolved_group
                app.cli.add_command(self.cli)

        for blueprint, bp_options in self._blueprints:
            bp_options = bp_options.copy()
            bp_url_prefix = bp_options.get("url_prefix")

            if bp_url_prefix is None:
                bp_url_prefix = blueprint.url_prefix

            if state.url_prefix is not None and bp_url_prefix is not None:
                bp_options["url_prefix"] = (
                    state.url_prefix.rstrip("/") + "/" + bp_url_prefix.lstrip("/")
                )
            elif bp_url_prefix is not None:
                bp_options["url_prefix"] = bp_url_prefix
            elif state.url_prefix is not None:
                bp_options["url_prefix"] = state.url_prefix

            bp_options["name_prefix"] = name
            blueprint.register(app, bp_options)

    def get(
        self,
        rule: str,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = [],
        **options: Any
    ) -> Callable:
        return self._method_route(
            "GET", rule, options, tags, summary, description, response_description,
            responses, auto_swagger, custom_swagger, security, dependencies
        )

    def post(
        self,
        rule: str,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = [],
        **options: Any
    ) -> Callable:
        return self._method_route(
            "POST", rule, options, tags, summary, description, response_description,
            responses, auto_swagger, custom_swagger, security, dependencies
        )
    
    def put(
        self,
        rule: str,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = [],
        **options: Any
    ) -> Callable:
        return self._method_route(
            "PUT", rule, options, tags, summary, description, response_description,
            responses, auto_swagger, custom_swagger, security, dependencies
        )

    def delete(
        self,
        rule: str,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = [],
        **options: Any
    ) -> Callable:
        return self._method_route(
            "DELETE", rule, options, tags, summary, description, response_description,
            responses, auto_swagger, custom_swagger, security, dependencies
        )
    
    def patch(
        self,
        rule: str,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = [],
        **options: Any
    ) -> Callable:
        return self._method_route(
            "PATCH", rule, options, tags, summary, description, response_description,
            responses, auto_swagger, custom_swagger, security, dependencies
        )
    
    def _method_route(
        self,
        method: str,
        rule: str,
        options: dict,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = []
    ) -> Callable:
        if "methods" in options:
            raise TypeError("Use the 'route' decorator to use the 'methods' argument")
        return self.route(
            rule=self.validate_rule(rule),
            methods=[method],
            tags=tags,
            summary=summary,
            description=description,
            response_description=response_description,
            responses=responses,
            auto_swagger=auto_swagger,
            custom_swagger=custom_swagger,
            security=security,
            dependencies=dependencies,
            **options
            )

    def add_url_rule(
        self,
        rule: str,
        endpoint: t.Optional[str] = None,
        view_func: t.Optional[t.Callable] = None,
        provide_automatic_options: t.Optional[bool] = None,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = [],
        **options: t.Any
    ) -> None:
        self.route(
            rule=self.validate_rule(rule),
            methods=options.pop("methods", ["GET"]),
            endpoint=endpoint,
            provide_automatic_options=provide_automatic_options,
            tags=tags,
            summary=summary,
            description=description,
            response_description=response_description,
            responses=responses,
            auto_swagger=auto_swagger,
            custom_swagger=custom_swagger,
            security=security,
            dependencies=dependencies,
            **options
        )(view_func)

    def route(
        self,
        rule: str,
        tags: Optional[List[str]] = [],
        summary: Optional[str] = None,
        description: Optional[str] = None,
        response_description: str = "Successful Response",
        responses: Optional[Dict[Union[int, str], Dict[str, Any]]] = None,
        auto_swagger: bool = True,
        custom_swagger: Optional[Dict[str, Any]] = None,
        security: Optional[HTTPSecurityBase] = None,
        dependencies: Optional[List[Callable]] = [],
        **options: Any
    ) -> Callable:

        assert rule[0] == "/", f"path rule must starts with '/' -> {rule}"

        security = self.security if not security else security
        
        self.update_dependencies(dependencies)
        
        def decorator(func: Callable) -> Callable:
            paired_params = self._get_func_signature(rule, func)
            aliases = self.get_params_aliases(paired_params)
            self.paired_signature[self.url_prefix+rule] = paired_params

            pydantic_model_no_body = self.generate_endpoint_pydantic(
                func.__name__+"Schema_no_Body", paired_params, with_body=False
            )
            pydantic_model = self.generate_endpoint_pydantic(
                func.__name__+"Schema", paired_params, with_body=True
            )

            def create_modified_func():
                @wraps(func)
                def modified_func(**paths):
                    try:
                        req = security(request) if security else request
                        if req.method == "GET":
                            valid_kwargs = self.get_kwargs(
                                paths, req, paired_params, pydantic_model_no_body, aliases
                            )
                        else:
                            valid_kwargs = self.get_kwargs(
                                paths, req, paired_params, pydantic_model, aliases
                            )
                        return func(**valid_kwargs)
                    except pydantic.ValidationError as e:
                        return JSONResponse(
                            response=e.errors(),
                            status_code=422
                        )
                    except Exception as e:
                        raise e
                return modified_func

            # register endpoint
            f = create_modified_func()
            endpoint = options.pop("endpoint", None)
            Blueprint.add_url_rule(self, rule, endpoint, f, **options)

            # register autoswagger
            for http_method in options.get("methods", ["GET"]):
                if http_method.upper() not in self.available_methods:
                    raise Exception(
                        f"Invalid Type of HTTP Method, expected between or/and : {self.available_methods}"
                    )

                defined_ep = EndpointDefinition(
                    rule=self.validate_rule_for_swagger(self.url_prefix+rule),
                    method=http_method,
                    paired_params=paired_params,
                    tags=tags+self.tags or ["default"],
                    summary=summary if summary else func.__name__,
                    description=description if description else func.__name__,
                    response_description=response_description,
                    responses=responses,
                    auto_swagger=self._enable_auto_swagger & auto_swagger,
                    custom_swagger=custom_swagger,
                    pydantic_model=pydantic_model,
                    security=security,
                    aliases=aliases
                )
                self.defined_endpoints.append(defined_ep)
            return func

        return decorator

    def fill_all_enum_value(self, o):
        try:
            datas = {}
            if type(o) == dict:
                for k in o:
                    datas[k] = self.fill_all_enum_value(o[k])
                return datas
            if enum.Enum.__subclasscheck__(o.__class__):
                return o.value
        except:
            return o
        return o
    
    def generate_endpoint_pydantic(self, name: str, paired_params: Dict[str, ParamSignature], with_body: bool = True):
        params = {
            key: (pp._type, pp.param_object.copy()) 
            for key, pp in paired_params.items()
        }
        if not with_body:
            for key in params:
                if isinstance(params[key][1], _BodyClasses):
                    params[key][1].disable_constraint()
        return create_model(name, __base__=BaseSchema, **params)

    def _get_func_signature(self, path: str, func: Callable) -> Dict[str, ParamSignature]:
        params_signature = inspect.signature(func).parameters
        annots = func.__annotations__
        pair = {}

        ## get params signature pair from function
        for k, p in params_signature.items():
            ## get default value
            if p.default != inspect._empty:
                if type(p.default) not in _ParamsClasses:
                    if type(p.default) == Depends:
                        try:
                            if k not in annots:
                                annots[k] = str
                            if not p.default.obj:
                                if k in annots:
                                    p.default.obj = annots[k]
                            if callable(p.default.obj):
                                pair.update(self._get_func_signature(path, p.default.obj))
                            continue
                        except:
                            default_value = Query(None)    
                    else:
                        default_value = Query(p.default)
                else:
                    default_value = p.default
            else:
                default_value = Query(...)

            ## check path params
            if self.check_params_in_path(k, path):
                default_value = Path(default_value.default)
            
            ## get default type
            if k in annots:
                default_type = annots[k]
                if type(default_value) in _FormClasses:
                    default_type = str if default_type == FileStorage else default_type
            else:
                if type(default_value) in _FormClasses:
                    default_type = Any
                else:
                    default_type = str
            
            ## check pydantic annots
            if type(default_value) not in _FormClasses:
                default_value = self.define_body_from_annots(default_value, default_type)

            pair[k] = ParamSignature(k, default_type, default_value)
        
        ## get params signature pairs from dependencies
        if self.dependecies:
            for dependency in self.dependecies:
                if callable(dependency):
                    pair.update(self._get_func_signature(path, dependency))
        return pair
    
    def define_body_from_annots(self, default_value, annot):
        pydantic_model = self.get_pydantic_from_annots(annot)
        if pydantic_model:
            return Body(default_value.default, pydantic_model=pydantic_model)
        else:
            return default_value
    
    def get_pydantic_from_annots(self, annot):
        try:
            if BaseModel.__subclasscheck__(annot):
                return annot
        except:
            pass
        if annot.__class__ in [t._GenericAlias, t._SpecialForm]:
            for a in annot.__args__:
                b = self.get_pydantic_from_annots(a)
                return b if b else annot

    def validate_rule_for_swagger(self, rule: str):
        opening_found = False
        new_rule = ""
        for i in range(len(rule)):
            if not opening_found and rule[i] == "<":
                opening_found = True
                new_rule += "{"
                continue
            if opening_found and rule[i] == ">":
                opening_found = False
                new_rule += "}"
                continue
            new_rule += rule[i]
        return new_rule
    
    def validate_rule(self, rule: str):
        pattern = re.compile(r"[<]{1}.*[>]{1}")
        for text in pattern.findall(rule):
            assert text.count(":") in [0,1], f"Multiple type definition using ':' in path -> {rule}"

        new_rule = ""
        start_write_path = False
        start_path = []
        for c in rule:
            if c == "<":
                start_path.append("")
                start_write_path = True
            elif c == ">":
                if start_path[-1].count(":"):
                    start_path[-1] = start_path[-1][start_path[-1].index(":")+1:]
                start_write_path = False
                new_rule += "<" + start_path[-1] + ">"
            else:
                if not start_write_path:
                    new_rule += c
                else:
                    start_path[-1] += c
        return new_rule

    def get_kwargs(
        self,
        paths: Dict[str, Any],
        request: Request,
        paired_params: Dict[str, ParamSignature],
        pydantic_model: BaseSchema,
        aliases: Dict[str, str]
    ):
        """Get keyword args that will be passed to the function
        """
        # path
        variables = pydantic_model.__fields__.keys()
        kwargs = {**paths}

        # query
        queries = request.args.to_dict()
        query_kwargs = {k: queries[k] for k in self.convert_alias_to_name(aliases["query"], variables) if k in queries}
        kwargs.update(query_kwargs)

        # header
        header_kwargs = {k: request.headers.get(k) for k in self.convert_alias_to_name(aliases["header"], variables) if request.headers.get(k)}
        kwargs.update(header_kwargs)

        # body
        if request.method != "GET":
            form_kwargs = {k: request.form.get(k) for k in self.convert_alias_to_name(aliases["form"], variables) if request.form.get(k)}
            file_kwargs = {k: request.files.get(k) for k in self.convert_alias_to_name(aliases["file"], variables) if request.files.get(k)}
            dummy_file_kwargs = {k: "__dummy" for k in file_kwargs}
            kwargs = {
                **form_kwargs,
                **dummy_file_kwargs
            }

        empty_keys = pydantic_model.get_non_exist_var_in_kwargs(**kwargs)
        total_body = self.count_required_body(paired_params)
        if total_body:
            for k in empty_keys:
                if k in paired_params:
                    po = paired_params[k].param_object

                    # JSON body
                    if type(po) == Body:
                        ak = po.alias or k
                        kwargs[k] = None
                        if request.method != "GET":
                            b = self.get_pydantic_from_annots(po.dtype)
                            if b:
                                if BaseModel.__subclasscheck__(b):
                                    if total_body == 1:
                                        kwargs[k] = b(**request.json)
                                    else:
                                        kwargs[k] = b(**request.json.get(ak, None))
                                else:
                                    kwargs[k] = request.json.get(ak, None)
                            else:
                                kwargs[k] = request.json.get(ak, None)
                            
    
        # mapping the kwargs
        valid_kwargs = pydantic_model(**kwargs)
        valid_kwargs = self.fill_all_enum_value(valid_kwargs)
        valid_kwargs = vars(valid_kwargs)

        # file kwargs should be placed after pydantic to make sure its not converted
        if request.method != "GET":
            valid_kwargs.update(file_kwargs)

        return valid_kwargs

    def get_params_aliases(self, paired_params: Dict[str, ParamSignature]) -> Dict[str, Dict[str, str]]:
        aliases = {
            "path": {},
            "header": {},
            "query": {},
            "body": {},
            "form": {},
            "file" :{}
        }
        for key, pp in paired_params.items():
            if isinstance(pp.param_object, Path):
                aliases["path"][key] = pp.param_object.alias or key
            elif isinstance(pp.param_object, Header):
                aliases["header"][key] = pp.param_object.alias or key
            elif isinstance(pp.param_object, Query):
                aliases["query"][key] = pp.param_object.alias or key
            elif isinstance(pp.param_object, Body):
                aliases["body"][key] = pp.param_object.alias or key
            elif isinstance(pp.param_object, (Form, FormURLEncoded)):
                aliases["form"][key] = pp.param_object.alias or key
            elif isinstance(pp.param_object, File):
                aliases["file"][key] = pp.param_object.alias or key
        return aliases
    
    def convert_alias_to_name(self, aliases: Dict[str, str], input_name: List[str]):
        converted_name = [aliases[key] for key in input_name if key in aliases]
        return converted_name

    def count_required_body(self, paired_params: Dict[str, Any]) -> int:
        total = 0
        for pp in paired_params.values():
            po = pp.param_object
            if type(po) == Body:
                total += 1
        return total

    def check_params_in_path(self, key: str, path: str):
        pattern = re.compile("[<]{1}"+key+"[>]{1}")
        matched_keys = pattern.findall(path)
        
        if len(matched_keys) == 1:
            return True
        elif len(matched_keys) > 1:
            error = f"Invalid path. multiple '{key}' appeared in : {path}"
            raise SwaggerPathError(error)
        return False
    
    def update_dependencies(self, stack: List[Callable]):
        for s in stack:
            if s not in self.dependecies:
                self.dependecies.append(s)