"""FastAPI application using exactly the same runtime as the CLI."""
from __future__ import annotations
import asyncio
import json
import os
import hmac
import time
from dataclasses import replace
from pathlib import Path
import tomllib
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import StreamingResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel,Field
from .config import settings,ROOT
from .contracts import SaveTripRequest
from .contracts import ChatRequest,SkillRequest,PreferenceRequest,SkillOutput
from .memory import MemoryStore
from .travel_preferences import (PROFILE_CATEGORY, TravelProfileWrite, TravelProfileReset,
                                 read_travel_profile, write_travel_profile,
                                 reset_travel_profile)
from .models import ModelGateway
from .model_management import ModelControl
from .providers import ProviderGateway,ProviderUnavailable
from .registry import SkillRegistry
from .retrieval import Retrieval
from .runtime import Runtime,SkillContext
from .security import (remote_owner_check, require_admin_token,
                       guard_worker_configuration, guard_database_process)


class DocumentRequest(BaseModel):
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=150000)

class ReindexRequest(BaseModel):
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    semantic: bool = True

def create_app(*, database_path=None, skill_root=None, models_file=None,
               model_presets_file=None, model_state_file=None):
    guard_worker_configuration()
    guard_database_process(database_path or settings.db_path)
    store=MemoryStore(database_path or settings.db_path)
    model_settings = (replace(settings, models_file=Path(models_file))
                      if models_file is not None else settings)
    control = ModelControl(model_settings, presets_file=model_presets_file,
                           state_file=model_state_file or
                           Path(database_path or settings.db_path).parent / 'active_model_preset.json')
    models=control.gateway
    providers=ProviderGateway(settings)
    registry=SkillRegistry(skill_root or settings.extensions_dir)
    retrieval=Retrieval(store,models,settings)
    runtime=Runtime(store,models,providers,registry,settings,retrieval=retrieval)
    project_version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    app=FastAPI(title='Aligo Travel Intelligence API',version=project_version,description='Extensible travel agent runtime with evidence-aware providers and honest capability states.')
    app.add_middleware(CORSMiddleware,allow_origins=list(settings.cors_origins),allow_credentials=False,allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Authorization','Content-Type','X-Aligo-Admin-Token'])
    app.state.store=store;app.state.models=models;app.state.providers=providers;app.state.registry=registry;app.state.runtime=runtime;app.state.retrieval=retrieval
    app.state.model_control = control
    model_admin_lock = asyncio.Lock()

    def model_admin_required(request: Request):
        require_admin_token(request, 'ALIGO_ENABLE_MODEL_ADMIN',
                            'ALIGO_MODEL_ADMIN_TOKEN')

    def skill_admin_required(request: Request):
        require_admin_token(request, 'ALIGO_ENABLE_SKILL_ADMIN',
                            'ALIGO_SKILL_ADMIN_TOKEN')

    def prepare_runtime(new_gateway):
        new_retrieval = Retrieval(store, new_gateway, settings)
        new_runtime = Runtime(store, new_gateway, providers, registry, settings,
                              retrieval=new_retrieval)
        return new_retrieval, new_runtime

    def change_gateway(new_gateway, prepared=None):
        nonlocal models, runtime, retrieval
        # Build dependencies before activation. A failed construction must not
        # change the persisted selection or alter an in-flight request.
        new_retrieval, new_runtime = prepared or prepare_runtime(new_gateway)
        models, retrieval, runtime = new_gateway, new_retrieval, new_runtime
        app.state.models = new_gateway
        app.state.retrieval = new_retrieval
        app.state.runtime = new_runtime

    @app.middleware('http')
    async def local_security(request:Request,call_next):
        # A CORS preflight never invokes application endpoints. Its credential-
        # free OPTIONS request must reach CORSMiddleware for inspection.
        if (request.method == 'OPTIONS' and request.headers.get('origin') and
                request.headers.get('access-control-request-method')):
            return await call_next(request)
        if request.url.path.startswith('/api/') and request.url.path!='/api/health':
            origin = request.headers.get('origin')
            if origin and origin not in settings.cors_origins:
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail': 'Unapproved browser origin.'}, status_code=403)
            supplied=request.headers.get('Authorization','')
            if settings.access_token:
                if supplied!=f'Bearer {settings.access_token}':
                    from fastapi.responses import JSONResponse
                    return JSONResponse({'detail':'Valid access token required.'},status_code=401)
            elif request.client and request.client.host not in ('127.0.0.1','::1','localhost','testclient'):
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail':'Remote access requires ALIGO_ACCESS_TOKEN.'},status_code=403)
            if (request.client and request.client.host not in
                    ('127.0.0.1', '::1', 'localhost', 'testclient') and
                    os.getenv('ALIGO_ALLOW_SHARED_REMOTE', '').lower() not in ('1', 'true', 'yes')):
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail': 'Remote shared-token mode is disabled. Dedicated user identity and isolation are required for multiuser hosting.'}, status_code=403)
            if request.client and request.client.host not in ('127.0.0.1', '::1', 'localhost', 'testclient'):
                # Shared remote access is single-owner only, not multi-tenant
                # authentication. Validate user identity on every scoped path.
                rejection = await remote_owner_check(request)
                if rejection is not None:
                    return rejection
        return await call_next(request)

    @app.get('/api/health')
    async def health():
        skills=registry.list()
        return {'status':'ok','version':app.version,'skill_count':len(skills),'implemented_skills':sum(s['release_state']=='implemented' for s in skills),'planned_skills':sum(s['release_state']=='planned' for s in skills),'models':app.state.models.catalog(),'providers':providers.catalog(),'memory_window':settings.memory_window,'max_parallel_skills':settings.max_parallel_skills,'max_recovery_steps':settings.max_recovery_steps,'execution_timeout':settings.execution_timeout,'intent_timeout':settings.intent_timeout,'online_enabled':settings.allow_online,'retrieval_mode':settings.retrieval_mode,'agent_mode':settings.agent_mode}

    @app.get('/api/diagnostics/skills')
    async def skill_diagnostics():
        return {'skills': registry.metrics(),
                'providers': {'network_calls': providers.external_calls,
                              'candidate_cache_hits': providers.cache_hits,
                              'estimated_provider_cost_usd': None},
                'online_enabled': settings.allow_online}

    @app.get('/api/skills')
    async def skills():return {'skills':registry.list(),'count':len(registry.specs)}

    @app.get('/api/models')
    async def model_profiles():
        value = control.public_state()
        value['management_enabled'] = (os.getenv('ALIGO_ENABLE_MODEL_ADMIN', '').lower()
                                       in ('1', 'true', 'yes') and
                                       bool(os.getenv('ALIGO_MODEL_ADMIN_TOKEN', '')))
        return value

    @app.get('/api/models/presets')
    async def model_presets():
        return {'presets': control.presets(), 'active_preset': control.active_preset}

    @app.post('/api/models/profiles', status_code=201)
    async def add_model_profile(profile: dict, request: Request):
        model_admin_required(request)
        async with model_admin_lock:
            try:
                result = control.add_profile(profile)
                change_gateway(control.build_gateway(control.active_preset))
                return result
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.delete('/api/models/profiles/{name}')
    async def remove_model_profile(name: str, request: Request):
        model_admin_required(request)
        async with model_admin_lock:
            try:
                result = control.remove_profile(name)
                change_gateway(control.build_gateway(control.active_preset))
                return result
            except KeyError as exc:
                raise HTTPException(404, 'Unknown profile.') from exc
            except (ValueError, TypeError) as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post('/api/models/presets', status_code=201)
    async def create_model_preset(payload: dict, request: Request):
        model_admin_required(request)
        async with model_admin_lock:
            try:
                if set(payload) - {'name', 'stages', 'description'}:
                    raise ValueError('Unsupported preset fields.')
                return control.add_preset(payload.get('name'), payload.get('stages'),
                                          description=payload.get('description', ''))
            except (ValueError, TypeError, KeyError) as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.post('/api/models/check')
    async def check_model_preset(payload: dict, request: Request):
        model_admin_required(request)
        try:
            if set(payload) - {'preset', 'stages', 'connect', 'allow_remote'}:
                raise ValueError('Unsupported compatibility check fields.')
            if type(payload.get('connect', False)) is not bool or type(payload.get('allow_remote', False)) is not bool:
                raise ValueError('connect and allow_remote must be boolean.')
            stages = payload.get('stages')
            if stages is not None and (not isinstance(stages, list) or
                                       any(not isinstance(stage, str) for stage in stages)):
                raise ValueError('Invalid stage selection.')
            return await control.check_compatibility(payload.get('preset'), stages,
                connect=payload.get('connect', False), allow_remote=payload.get('allow_remote', False))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/api/models/activate')
    async def activate_model_preset(payload: dict, request: Request):
        model_admin_required(request)
        async with model_admin_lock:
            try:
                if set(payload) - {'preset', 'allow_remote'}:
                    raise ValueError('Unsupported activation fields.')
                if type(payload.get('allow_remote', False)) is not bool:
                    raise ValueError('allow_remote must be boolean.')
                preset = payload.get('preset')
                gateway, remote = control.prepare_activation(
                    preset, allow_remote=payload.get('allow_remote', False))
                # Only persist after all validations and runtime construction.
                # New chats see the new gateway; in-flight chats retain theirs.
                prepared = prepare_runtime(gateway)
                control.commit_activation(preset, gateway, remote)
                change_gateway(gateway, prepared=prepared)
                return {'activated': preset, 'stage_models': gateway.selected_profiles(),
                        'remote_stages': remote, 'verification': 'configuration_only'}
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.get('/api/providers')
    async def provider_profiles():return {'providers':providers.catalog()}

    @app.post('/api/admin/reload-skills')
    async def reload_skills(request: Request):
        skill_admin_required(request)
        async with model_admin_lock:
            registry.reload()
            return {'count':len(registry.specs)}

    @app.get('/api/evaluation/catalog')
    async def catalog_evaluation():
        from .evaluation import evaluate_catalog, load_suite
        path = ROOT / 'evaluations' / 'catalog_v1.json'
        return evaluate_catalog(registry, load_suite(path), maximum=settings.max_skill_calls)

    @app.get('/api/sessions')
    async def sessions(user_id:str='local-demo'):return {'sessions':store.sessions(user_id)}

    @app.get('/api/history')
    async def history(user_id:str='local-demo',session_id:str='default'):return {'messages':store.messages(user_id,session_id,settings.memory_window*2)}

    @app.get('/api/trips/active')
    async def active_trip(user_id:str='local-demo',session_id:str='default'):
        return {'active_itinerary':store.active_itinerary(user_id,session_id),
                'working_draft':store.working_itinerary(user_id,session_id),
                'versions':store.itinerary_versions(user_id,session_id),
                'draft_versions':store.working_itinerary_versions(user_id,session_id),
                'draft_history':store.working_itinerary_history(user_id,session_id),
                'saved_snapshots':store.saved_trip_snapshots(user_id,session_id)}

    @app.post('/api/trips/save')
    async def save_current_trip(request: SaveTripRequest):
        # Accept both public version-guard field names from prior releases.
        version = request.expected_verified_version
        if version is None:
            version = request.expected_version
        if version is None or (request.expected_version is not None and
                               request.expected_verified_version is not None and
                               request.expected_version != request.expected_verified_version):
            raise HTTPException(422, 'A consistent expected itinerary version is required.')
        from .working_drafts import saved_response
        try:
            snapshot = store.save_visible_itinerary(
                request.user_id, request.session_id,
                expected_verified_version=version,
                expected_draft_version=request.expected_draft_version)
        except ValueError as exc:
            if 'No itinerary exists' in str(exc):
                raise HTTPException(404, str(exc)) from exc
            raise HTTPException(409, str(exc)) from exc
        active = store.active_itinerary(request.user_id, request.session_id)
        draft = store.working_itinerary(request.user_id, request.session_id)
        output = saved_response({
            'saved_snapshot': snapshot,
            'saved_id': snapshot['saved_id'],
            'source_status': snapshot['source_status'],
            'source_version': snapshot['source_version'],
            'saved_at': snapshot['saved_at'],
            'already_saved': bool(snapshot.get('already_saved')),
            'saved_status': 'draft' if snapshot['source_status'] == 'draft' else 'validated',
            'active_itinerary': active, 'working_draft': draft,
            'draft_history': store.working_itinerary_history(request.user_id, request.session_id),
            'saved_snapshots': store.saved_trip_snapshots(request.user_id, request.session_id),
        }, request.session_id)
        # A repeated Save is idempotent: neither a new copy nor duplicate
        # administrative conversation messages should be created.
        if not snapshot.get('already_saved'):
            store.add_message(request.user_id, request.session_id, 'user', 'Save my itinerary.')
            store.add_message(request.user_id, request.session_id, 'assistant', output['answer'])
        return output

    @app.get('/api/trips/drafts/{version}')
    @app.get('/api/trips/draft/{version}')
    async def draft_version(version: int, user_id: str = 'local-demo', session_id: str = 'default'):
        document = store.working_itinerary_version(user_id, session_id, version)
        if document is None:
            raise HTTPException(404, 'Draft version not found.')
        return {'working_draft': document, **document}

    @app.get('/api/trips/saved/{snapshot_id}')
    async def saved_trip(snapshot_id: int, user_id: str = 'local-demo', session_id: str = 'default'):
        snapshot = store.saved_trip_snapshot(user_id, session_id, snapshot_id)
        if snapshot is None:
            raise HTTPException(404, 'Saved itinerary not found.')
        return snapshot

    @app.get('/api/travel-profile')
    async def get_travel_profile(user_id: str = 'local-demo'):
        return read_travel_profile(store, user_id)

    @app.put('/api/travel-profile')
    async def put_travel_profile(request: TravelProfileWrite):
        return write_travel_profile(store, request.user_id, request.profile)

    @app.post('/api/travel-profile/reset')
    async def clear_travel_profile(request: TravelProfileReset):
        return reset_travel_profile(store, request.user_id)

    @app.get('/api/preferences')
    async def prefs(user_id:str='local-demo',session_id:str='default'):return {'preferences':store.preferences(user_id,session_id)}

    @app.post('/api/preferences')
    async def save_pref(request:PreferenceRequest):
        if request.category == PROFILE_CATEGORY:
            raise HTTPException(400, 'Use /api/travel-profile for structured preferences.')
        try:result=store.update_preference(request.user_id,request.category,request.value,request.scope,request.session_id,request.consent)
        except PermissionError as exc:raise HTTPException(403,str(exc)) from exc
        return {'preferences':result}

    @app.delete('/api/preferences/{category}')
    async def delete_pref(category:str,user_id:str='local-demo',scope:str='global',session_id:str='default'):
        if category == PROFILE_CATEGORY:
            raise HTTPException(400, 'Use /api/travel-profile/reset to clear the travel profile.')
        store.delete_preference(user_id,category,scope,session_id);return {'deleted':True}

    @app.delete('/api/users/{user_id}/data')
    async def delete_user(user_id:str):
        store.delete_user(user_id);return {'deleted':True}

    @app.get('/api/knowledge')
    async def knowledge(user_id:str='local-demo'):
        docs=store.documents(user_id)
        return {'documents':[{'title':d['title'],'id':d['id'],'size':len(d['content'])} for d in docs],'count':len(docs)}

    @app.post('/api/knowledge/documents')
    async def add_document(request:DocumentRequest):
        document_id=store.add_document(request.user_id,request.title,request.content)
        report=await asyncio.to_thread(retrieval.index_document,request.user_id,document_id)
        return {'created':True,'document_id':document_id,'index':report}

    @app.put('/api/knowledge/documents/{document_id}')
    async def update_document(document_id:int,request:DocumentRequest):
        # Invalidate old chunks before replacing the source document.
        if not store.get_document(request.user_id,document_id):
            raise HTTPException(404,'Document was not found for this user.')
        store.update_document(request.user_id,document_id,request.title,request.content)
        report=await asyncio.to_thread(retrieval.index_document,request.user_id,document_id)
        return {'updated':True,'document_id':document_id,'index':report}

    @app.delete('/api/knowledge/documents/{document_id}')
    async def delete_document(document_id:int,user_id:str='local-demo'):
        if not store.get_document(user_id,document_id):
            raise HTTPException(404,'Document was not found for this user.')
        store.delete_document(user_id,document_id)
        return {'deleted':True,'document_id':document_id}

    @app.get('/api/knowledge/index/status')
    async def knowledge_index_status(user_id:str='local-demo'):
        return retrieval.index.stats(user_id,models.embedding_model_id())

    @app.post('/api/knowledge/reindex')
    async def knowledge_reindex(request:ReindexRequest):
        return await asyncio.to_thread(retrieval.reindex,request.user_id,semantic=request.semantic)

    @app.get('/api/knowledge/search')
    async def knowledge_search(query:str,user_id:str='local-demo',k:int=5):
        return await asyncio.to_thread(retrieval.search,user_id,query,k)

    @app.post('/api/chat')
    async def chat(request:ChatRequest):
        current_runtime = app.state.runtime
        return await current_runtime.execute(request.user_id,request.session_id,request.message,request.context)

    @app.post('/api/skills/{skill_id}/run')
    async def direct_run(skill_id:str,request:SkillRequest):
        if not registry.get(skill_id):raise HTTPException(404,'Unknown skill.')
        context=SkillContext(request.user_id,request.session_id,request.query,request.arguments,store,app.state.models,providers,retrieval=app.state.retrieval)
        try:
            out=await asyncio.wait_for(registry.run(skill_id,context,request.arguments),timeout=registry.get(skill_id).timeout_seconds)
            out.name=skill_id
            return out
        except ProviderUnavailable as exc:return SkillOutput(name=skill_id,status='unavailable',text=str(exc),error_code='PROVIDER_UNAVAILABLE')
        except (ValueError,KeyError) as exc:return SkillOutput(name=skill_id,status='partial',text=f'Invalid input: {exc}',error_code='INVALID_INPUT')
        except asyncio.TimeoutError:return SkillOutput(name=skill_id,status='unavailable',text='Skill request timed out.',error_code='TIMEOUT')

    @app.post('/api/chat/stream')
    async def chat_stream(request:ChatRequest):
        current_runtime = app.state.runtime
        async def iterate():
            try:
                capacity = max(8, min(int(os.getenv('ALIGO_SSE_QUEUE_SIZE', '64')), 512))
                heartbeat = max(3., min(float(os.getenv('ALIGO_SSE_HEARTBEAT_SECONDS', '15')), 60.))
            except ValueError:
                capacity, heartbeat = 64, 15.
            queue = asyncio.Queue(maxsize=capacity)
            # Preserve every event. Backpressure slows the producer rather
            # than silently losing the final done event or a failure notice.
            async def send(name, payload):
                await queue.put((name, payload))
            task=asyncio.create_task(current_runtime.execute(request.user_id,request.session_id,request.message,request.context,on_event=send))
            try:
                while not task.done() or not queue.empty():
                    try:
                        name,payload=await asyncio.wait_for(queue.get(),timeout=heartbeat)
                        yield f'event: {name}\ndata: {json.dumps(payload,ensure_ascii=False,default=str)}\n\n'
                    except asyncio.TimeoutError:
                        yield 'event: heartbeat\ndata: {}\n\n'
                        continue
                try:await task
                except Exception:yield 'event: error\ndata: {"message":"The request could not be completed."}\n\n'
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(iterate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    dist=ROOT/'web'/'dist'
    if (dist/'index.html').exists():
        if (dist/'assets').exists():app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
        @app.get('/')
        @app.get('/{path:path}')
        async def frontend(path:str=''):
            if path.startswith('api/'):raise HTTPException(404,'Not found')
            return FileResponse(dist/'index.html')
    return app


app=create_app()
