import logging
from typing import NoReturn

from flask import Response
from flask_restful import Resource, fields, inputs, marshal_with, reqparse
from sqlalchemy.orm import Session
from werkzeug.exceptions import Forbidden

from controllers.console import api
from controllers.console.app.error import (
    DraftWorkflowNotExist,
)
from controllers.console.app.wraps import get_app_model
from controllers.console.wraps import account_initialization_required, setup_required
from controllers.web.error import InvalidArgumentError, NotFoundError
from core.workflow.constants import CONVERSATION_VARIABLE_NODE_ID, SYSTEM_VARIABLE_NODE_ID
from factories.variable_factory import build_segment
from libs.login import current_user, login_required
from models import App, AppMode, db
from models.workflow import WorkflowDraftVariable
from services.workflow_draft_variable_service import WorkflowDraftVariableList, WorkflowDraftVariableService
from services.workflow_service import WorkflowService

logger = logging.getLogger(__name__)


def _create_pagination_parser():
    parser = reqparse.RequestParser()
    parser.add_argument(
        "page",
        type=inputs.int_range(1, 100_000),
        required=False,
        default=1,
        location="args",
        help="the page of data requested",
    )
    parser.add_argument("limit", type=inputs.int_range(1, 100), required=False, default=20, location="args")
    return parser


_WORKFLOW_DRAFT_VARIABLE_WITHOUT_VALUE_FIELDS = {
    "id": fields.String,
    "type": fields.String(attribute=lambda model: model.get_variable_type()),
    "name": fields.String,
    "description": fields.String,
    "selector": fields.List(fields.String, attribute=lambda model: model.get_selector()),
    "value_type": fields.String,
    "edited": fields.Boolean(attribute=lambda model: model.edited),
    "visible": fields.Boolean,
}

_WORKFLOW_DRAFT_VARIABLE_FIELDS = dict(
    _WORKFLOW_DRAFT_VARIABLE_WITHOUT_VALUE_FIELDS,
    value=fields.Raw(attribute=lambda variable: variable.get_value().value),
)

_WORKFLOW_DRAFT_ENV_VARIABLE_FIELDS = {
    "id": fields.String,
    "type": fields.String(attribute=lambda _: "env"),
    "name": fields.String,
    "description": fields.String,
    "selector": fields.List(fields.String, attribute=lambda model: model.get_selector()),
    "value_type": fields.String,
    "edited": fields.Boolean(attribute=lambda model: model.edited),
    "visible": fields.Boolean,
}

_WORKFLOW_DRAFT_ENV_VARIABLE_LIST_FIELDS = {
    "items": fields.List(fields.Nested(_WORKFLOW_DRAFT_ENV_VARIABLE_FIELDS)),
}


def _get_items(var_list: WorkflowDraftVariableList) -> list[WorkflowDraftVariable]:
    return var_list.variables


_WORKFLOW_DRAFT_VARIABLE_LIST_WITHOUT_VALUE_FIELDS = {
    "items": fields.List(fields.Nested(_WORKFLOW_DRAFT_VARIABLE_WITHOUT_VALUE_FIELDS), attribute=_get_items),
    "total": fields.Raw(),
}

_WORKFLOW_DRAFT_VARIABLE_LIST_FIELDS = {
    "items": fields.List(fields.Nested(_WORKFLOW_DRAFT_VARIABLE_FIELDS), attribute=_get_items),
}


def _api_prerequisite(f):
    """Common prerequisites for all draft workflow variable APIs.

    It ensures the following conditions are satisfied:

    - Dify has been property setup.
    - The request user has logged in and initialized.
    - The requested app is a workflow or a chat flow.
    - The request user has the edit permission for the app.
    """

    @setup_required
    @login_required
    @account_initialization_required
    @get_app_model(mode=[AppMode.ADVANCED_CHAT, AppMode.WORKFLOW])
    def wrapper(*args, **kwargs):
        if not current_user.is_editor:
            raise Forbidden()
        return f(*args, **kwargs)

    return wrapper


class WorkflowVariableCollectionApi(Resource):
    @_api_prerequisite
    @marshal_with(_WORKFLOW_DRAFT_VARIABLE_LIST_WITHOUT_VALUE_FIELDS)
    def get(self, app_model: App):
        """
        Get draft workflow
        """
        parser = _create_pagination_parser()
        args = parser.parse_args()

        # fetch draft workflow by app_model
        workflow_service = WorkflowService()
        workflow_exist = workflow_service.is_workflow_exist(app_model=app_model)
        if not workflow_exist:
            raise DraftWorkflowNotExist()

        # fetch draft workflow by app_model
        with Session(bind=db.engine, expire_on_commit=False) as session:
            draft_var_srv = WorkflowDraftVariableService(
                session=session,
            )
        workflow_vars = draft_var_srv.list_variables_without_values(
            app_id=app_model.id,
            page=args.page,
            limit=args.limit,
        )

        return workflow_vars

    @_api_prerequisite
    def delete(self, app_model: App):
        draft_var_srv = WorkflowDraftVariableService(
            session=db.session(),
        )
        draft_var_srv.delete_workflow_variables(app_model.id)
        db.session.commit()
        return Response("", 204)


def validate_node_id(node_id: str) -> NoReturn | None:
    if node_id in [
        CONVERSATION_VARIABLE_NODE_ID,
        SYSTEM_VARIABLE_NODE_ID,
    ]:
        # NOTE(QuantumGhost): While we store the system and conversation variables as node variables
        # with specific `node_id` in database, we still want to make the API separated. By disallowing
        # accessing system and conversation variables in `WorkflowDraftNodeVariableListApi`,
        # we mitigate the risk that user of the API depending on the implementation detail of the API.
        #
        # ref: [Hyrum's Law](https://www.hyrumslaw.com/)

        raise InvalidArgumentError(
            f"invalid node_id, please use correspond api for conversation and system variables, node_id={node_id}",
        )
    return None


class NodeVariableCollectionApi(Resource):
    @_api_prerequisite
    @marshal_with(_WORKFLOW_DRAFT_VARIABLE_LIST_FIELDS)
    def get(self, app_model: App, node_id: str):
        validate_node_id(node_id)
        with Session(bind=db.engine, expire_on_commit=False) as session:
            draft_var_srv = WorkflowDraftVariableService(
                session=session,
            )
            node_vars = draft_var_srv.list_node_variables(app_model.id, node_id)

        return node_vars

    @_api_prerequisite
    def delete(self, app_model: App, node_id: str):
        validate_node_id(node_id)
        srv = WorkflowDraftVariableService(db.session())
        srv.delete_node_variables(app_model.id, node_id)
        db.session.commit()
        return Response("", 204)


class VariableApi(Resource):
    _PATCH_NAME_FIELD = "name"
    _PATCH_VALUE_FIELD = "value"

    @_api_prerequisite
    @marshal_with(_WORKFLOW_DRAFT_VARIABLE_FIELDS)
    def get(self, app_model: App, variable_id: str):
        draft_var_srv = WorkflowDraftVariableService(
            session=db.session(),
        )
        variable = draft_var_srv.get_variable(variable_id=variable_id)
        if variable is None:
            raise NotFoundError(description=f"variable not found, id={variable_id}")
        if variable.app_id != app_model.id:
            raise NotFoundError(description=f"variable not found, id={variable_id}")
        return variable

    @_api_prerequisite
    @marshal_with(_WORKFLOW_DRAFT_VARIABLE_FIELDS)
    def patch(self, app_model: App, variable_id: str):
        parser = reqparse.RequestParser()
        parser.add_argument(self._PATCH_NAME_FIELD, type=str, required=False, nullable=True, location="json")
        parser.add_argument(self._PATCH_VALUE_FIELD, type=build_segment, required=False, nullable=True, location="json")

        draft_var_srv = WorkflowDraftVariableService(
            session=db.session(),
        )
        args = parser.parse_args(strict=True)

        variable = draft_var_srv.get_variable(variable_id=variable_id)
        if variable is None:
            raise NotFoundError(description=f"variable not found, id={variable_id}")
        if variable.app_id != app_model.id:
            raise NotFoundError(description=f"variable not found, id={variable_id}")

        new_name = args.get(self._PATCH_NAME_FIELD, None)
        new_value = args.get(self._PATCH_VALUE_FIELD, None)

        if new_name is None and new_value is None:
            return variable
        draft_var_srv.update_variable(variable, name=new_name, value=new_value)
        db.session.commit()
        return variable

    @_api_prerequisite
    def delete(self, app_model: App, variable_id: str):
        draft_var_srv = WorkflowDraftVariableService(
            session=db.session(),
        )
        variable = draft_var_srv.get_variable(variable_id=variable_id)
        if variable is None:
            raise NotFoundError(description=f"variable not found, id={variable_id}")
        if variable.app_id != app_model.id:
            raise NotFoundError(description=f"variable not found, id={variable_id}")
        draft_var_srv.delete_variable(variable)
        db.session.commit()
        return Response("", 204)


class VariableResetApi(Resource):
    @_api_prerequisite
    @marshal_with(_WORKFLOW_DRAFT_VARIABLE_FIELDS)
    def put(self, app_model: App, variable_id: str):
        draft_var_srv = WorkflowDraftVariableService(
            session=db.session(),
        )

        workflow_srv = WorkflowService()
        draft_workflow = workflow_srv.get_draft_workflow(app_model)
        if draft_workflow is None:
            raise NotFoundError(
                f"Draft workflow not found, app_id={app_model.id}",
            )
        variable = draft_var_srv.get_variable(variable_id=variable_id)
        if variable is None:
            raise NotFoundError(description=f"variable not found, id={variable_id}")
        if variable.app_id != app_model.id:
            raise NotFoundError(description=f"variable not found, id={variable_id}")

        if variable.node_id != CONVERSATION_VARIABLE_NODE_ID:
            error_msg = "variable is not a conversation variable, id={}, node_id={},  name={}".format(
                variable.id,
                variable.node_id,
                variable.name,
            )
            raise InvalidArgumentError(error_msg)
        resetted = draft_var_srv.reset_conversation_variable(draft_workflow, variable)
        db.session.commit()
        if resetted is None:
            return Response("", 204)
        else:
            return variable


def _get_variable_list(app_model: App, node_id) -> WorkflowDraftVariableList:
    with Session(bind=db.engine, expire_on_commit=False) as session:
        draft_var_srv = WorkflowDraftVariableService(
            session=session,
        )
        if node_id == CONVERSATION_VARIABLE_NODE_ID:
            draft_vars = draft_var_srv.list_conversation_variables(app_model.id)
        elif node_id == SYSTEM_VARIABLE_NODE_ID:
            draft_vars = draft_var_srv.list_system_variables(app_model.id)
        else:
            draft_vars = draft_var_srv.list_node_variables(app_id=app_model.id, node_id=node_id)
    return draft_vars


class ConversationVariableCollectionApi(Resource):
    @_api_prerequisite
    @marshal_with(_WORKFLOW_DRAFT_VARIABLE_LIST_FIELDS)
    def get(self, app_model: App):
        # NOTE(QuantumGhost): Prefill conversation variables into the draft variables table
        # so their IDs can be returned to the caller.
        workflow_srv = WorkflowService()
        draft_workflow = workflow_srv.get_draft_workflow(app_model)
        if draft_workflow is None:
            raise NotFoundError(description=f"draft workflow not found, id={app_model.id}")
        draft_var_srv = WorkflowDraftVariableService(db.session())
        draft_var_srv.prefill_conversation_variable_default_values(draft_workflow)
        db.session.commit()
        return _get_variable_list(app_model, CONVERSATION_VARIABLE_NODE_ID)


class SystemVariableCollectionApi(Resource):
    @_api_prerequisite
    @marshal_with(_WORKFLOW_DRAFT_VARIABLE_LIST_FIELDS)
    def get(self, app_model: App):
        return _get_variable_list(app_model, SYSTEM_VARIABLE_NODE_ID)


class EnvironmentVariableCollectionApi(Resource):
    @_api_prerequisite
    def get(self, app_model: App):
        """
        Get draft workflow
        """
        # fetch draft workflow by app_model
        workflow_service = WorkflowService()
        workflow = workflow_service.get_draft_workflow(app_model=app_model)
        if workflow is None:
            raise DraftWorkflowNotExist()

        env_vars = workflow.environment_variables
        env_vars_list = []
        for v in env_vars:
            env_vars_list.append(
                {
                    "id": v.id,
                    "type": "env",
                    "name": v.name,
                    "description": v.description,
                    "selector": v.selector,
                    "value_type": v.value_type.value,
                    "value": v.value,
                    # Do not track edited for env vars.
                    "edited": False,
                    "visible": True,
                    "editable": True,
                }
            )

        return {"items": env_vars_list}


api.add_resource(
    WorkflowVariableCollectionApi,
    "/apps/<uuid:app_id>/workflows/draft/variables",
)
api.add_resource(NodeVariableCollectionApi, "/apps/<uuid:app_id>/workflows/draft/nodes/<string:node_id>/variables")
api.add_resource(VariableApi, "/apps/<uuid:app_id>/workflows/draft/variables/<uuid:variable_id>")
api.add_resource(VariableResetApi, "/apps/<uuid:app_id>/workflows/draft/variables/<uuid:variable_id>/reset")

api.add_resource(ConversationVariableCollectionApi, "/apps/<uuid:app_id>/workflows/draft/conversation-variables")
api.add_resource(SystemVariableCollectionApi, "/apps/<uuid:app_id>/workflows/draft/system-variables")
api.add_resource(EnvironmentVariableCollectionApi, "/apps/<uuid:app_id>/workflows/draft/environment-variables")
