import re
import sys
import tempfile
import contextlib

import synapse.exc as s_exc
import synapse.cortex as s_cortex

import synapse.lib.ast as s_ast
import synapse.lib.parser as s_parser
import synapse.lib.stormtypes as s_stormtypes

from pygls.server import LanguageServer
from pygls.workspace import TextDocument

from lsprotocol import types

server = LanguageServer("storm-glass-server", "v0.0.1")


WORD = re.compile(r'\$?[\w\:\.]+')


class StormLanguageServer(LanguageServer):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics = {}
        self.query = None
        self.completions = {}

    async def loadCompletions(self, core):
        self.completions = {
            'libs': {},
            'formtypes': {},
            'props': {},
            'cmds': {},
        }
        for lib in s_stormtypes.registry.getLibDocs():
            base = '.'.join(lib['path'])
            for lcl in lib['locals']:
                name = lcl['name']
                key = '$' + '.'.join((base, name))
                self.completions['libs'][key] = lcl

        model = await core.getModelDict()

        for formtype, typeinfo in model.get('types', {}).items():
            self.completions['formtypes'][formtype] = typeinfo['info']['doc']

        for form, info in model.get('forms', {}).items():
            for propname, propinfo in info['props'].items():
                self.completions['props'][propinfo['full']] = propinfo.get('doc', '')

        for name, ctor in core.stormcmds.items():
            doc = ctor.getCmdBrief()
            self.completions['cmds'][name] = doc

    def parse(self, document: TextDocument):
        diagnostics = []

        try:
            query = s_parser.parseQuery(document.source)
            self.query = query
        except s_exc.BadSyntax as e:
            items = e.items()
            token = items.get('token', '1')
            message = items['mesg']
            severity = types.DiagnosticSeverity.Error
            diagnostics.append(
                types.Diagnostic(
                    message=message,
                    severity=severity,
                    range=types.Range(
                        start=types.Position(line=items['line'], character=items['column']-1),
                        end=types.Position(line=items['line'], character=items['column'] + len(token)),
                    ),
                )
            )

        self.diagnostics[document.uri] = (document.version, diagnostics)


server = StormLanguageServer("diagnostic-server", "v1")


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
async def did_change(ls: StormLanguageServer, params: types.DidOpenTextDocumentParams):
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.parse(doc)

    for uri, (version, diagnostics) in ls.diagnostics.items():
        ls.publish_diagnostics(uri=uri, version=version, diagnostics=diagnostics)


@server.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
async def document_symbol(ls: StormLanguageServer, params: types.DocumentSymbolParams):
    if not ls.query:
        return None

    retn = []

    for kid in ls.query.kids:
        if isinstance(kid, s_ast.Function):
            pos = kid.getPosInfo()
            retn.append(
                types.DocumentSymbol(
                    name=kid.kids[0].value(),
                    kind=types.SymbolKind.Function,
                    range=types.Range(
                        start=types.Position(line=pos['lines'][0]-1, character=0),
                        end=types.Position(line=pos['lines'][1]-1, character=0),
                    ),
                    selection_range=types.Range(
                        start=types.Position(line=pos['lines'][0]-1, character=0),
                        end=types.Position(line=pos['lines'][1]-1, character=0),
                    )
                )
            )
        elif isinstance(kid, s_ast.SetVarOper):
            pos = kid.getPosInfo()
            retn.append(
                types.DocumentSymbol(
                    name=kid.kids[0].value(),
                    kind=types.SymbolKind.Variable,
                    range=types.Range(
                        start=types.Position(line=pos['lines'][0]-1, character=0),
                        end=types.Position(line=pos['lines'][1]-1, character=0),
                    ),
                    selection_range=types.Range(
                        start=types.Position(line=pos['lines'][0]-1, character=0),
                        end=types.Position(line=pos['lines'][1]-1, character=0),
                    )
                )
            )

    return retn


@contextlib.asynccontextmanager
async def getTestCore():
    # It's an annoying startup cost, but it's a pretty dumb simple way to get the default model defs
    # TODO: so if we had a cortex connection we could reach out and also autocomplete
    # package names and stormcmds, non-default model elements, but that might be a tad touchy to do
    # because I don't wanna touch cred storing
    conf = {
        'health:sysctl:checks': False,
    }
    # Defer pulling in the autocompletes because start_io starts its own asyncio loop which causes
    # issues if we start our own
    with tempfile.TemporaryDirectory() as dirn:
        async with await s_cortex.Cortex.anit(dirn, conf=conf) as core:
            yield core


@server.feature(types.INITIALIZE)
async def lsinit(ls: StormLanguageServer, params: types.InitializeParams):
    async with getTestCore() as core:
        await ls.loadCompletions(core)
    ls.show_message('storm ready')


def wordAtCursor(lineNum, line, charAt):
    for match in WORD.finditer(line):
        start = match.start()
        end = match.end()
        if start <= charAt <= end:
            return (line[start:end], types.Range(
                start=types.Position(line=lineNum, character=start),
                end=types.Position(line=lineNum, character=end),
            ))

    return None


@server.feature(types.TEXT_DOCUMENT_COMPLETION, types.CompletionOptions(trigger_characters=[".", ':']))
async def autocomplete(ls: StormLanguageServer, params: types.CompletionParams):
    uri = params.text_document.uri
    doc = ls.workspace.get_text_document(uri)

    if params.position is None:
        return

    line = params.position.line
    atCursor = wordAtCursor(line, doc.lines[line], params.position.character)

    retn = []
    if atCursor:
        word, rng = atCursor
        if word[0] == '$':
            for name, valu in ls.completions.get('libs', {}).items():
                if name.startswith(word):
                    kind = types.CompletionItemKind.Property
                    if isinstance(valu.get('type'), dict):
                        if valu['type'].get('type') == 'function':
                            kind = types.CompletionItemKind.Function
                    retn.append(
                        types.CompletionItem(
                            label=name,
                            kind=kind,
                            detail=valu.get('desc'),
                            text_edit=types.TextEdit(
                                new_text=name,
                                range=rng,
                            )
                        )
                    )

            # TODO: detect what function we're in and populate variables based on that
            # also add global variables to this
            for kid in ls.query.kids:
                if isinstance(kid, s_ast.Function):
                    name = f'${kid.kids[0].value()}'
                    if name.startswith(word):
                        retn.append(
                            types.CompletionItem(
                                label=name,
                                kind=types.CompletionItemKind.Function,
                                text_edit=types.TextEdit(
                                    new_text=name,
                                    range=rng,
                                )
                            )
                        )

                    pos = kid.getPosInfo()
                    start, end = pos['lines']
                    if start <= line < end:
                        # TODO: we could also recurse down and find any SetVar opers?
                        funcargs = [f'${p.value()}' for p in kid.kids[1].kids]
                        # TODO: Like the issue noted later with commands, we could add our own completion
                        # type here for parameter (or perhaps that's better left to semantic highlighting?)
                        for arg in funcargs:
                            if arg.startswith(word):
                                retn.append(
                                    types.CompletionItem(
                                        label=arg,
                                        kind=types.CompletionItemKind.Variable,
                                        text_edit=types.TextEdit(
                                            new_text=arg,
                                            range=rng,
                                        )
                                    )
                                )

        else:
            text = word.strip()

            # if it's dumb but it works, how dumb is it really?
            formtypes = ls.completions.get('formtypes', {})
            for name, valu in formtypes.items():
                if name.startswith(text):
                    retn.append(
                        types.CompletionItem(
                            label=name,
                            kind=types.CompletionItemKind.Field,
                            detail=valu,
                            text_edit=types.TextEdit(
                                new_text=name,
                                range=rng,
                            )
                        )
                    )
            props = ls.completions.get('props', {})
            for name, valu in props.items():
                if name.startswith(text):
                    retn.append(
                        types.CompletionItem(
                            label=name,
                            kind=types.CompletionItemKind.Property,
                            detail=valu,
                            text_edit=types.TextEdit(
                                new_text=name,
                                range=rng,
                            )
                        )
                    )

            cmds = ls.completions.get('cmds', {})
            for name, desc in cmds.items():
                if name.startswith(text):
                    # TODO: as part of the LS protocol python pack we could define a custom type
                    # and use that here, but it's not yet in a proper release, so for now we
                    # gotta go with something not as accurate.
                    retn.append(
                        types.CompletionItem(
                            label=name,
                            kind=types.CompletionItemKind.Function,
                            detail=desc,
                            text_edit=types.TextEdit(
                                new_text=name,
                                range=rng
                            )
                        )
                    )

            # TODO: Keywords?

    return types.CompletionList(is_incomplete=False, items=retn)


def main(argv):
    server.start_io()


if __name__ == '__main__':
    main(sys.argv[1:])
