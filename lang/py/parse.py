import ast
import re
import astor
import logging
from cStringIO import StringIO
from tokenize import generate_tokens, tokenize
import token as tk

from astnode import ASTNode
from lang.util import typename
from lang.py.grammar import is_compositional_leaf, PY_AST_NODE_FIELDS, NODE_FIELD_BLACK_LIST, PythonGrammar
from lang.util import escape

def python_ast_to_parse_tree(node):
    assert isinstance(node, ast.AST)

    node_type = type(node)
    tree = ASTNode(node_type)

    # it's a leaf AST node, e.g., ADD, Break, etc.
    if len(node._fields) == 0:
        return tree

    # if it's a compositional AST node with empty fields
    if is_compositional_leaf(node):
        epsilon = ASTNode('epsilon')
        tree.add_child(epsilon)

        return tree

    fields_info = PY_AST_NODE_FIELDS[node_type.__name__]

    for field_name, field_value in ast.iter_fields(node):
        # remove ctx stuff
        if field_name in NODE_FIELD_BLACK_LIST:
            continue

        # omit empty fields, including empty lists
        if field_value is None or (isinstance(field_value, list) and len(field_value) == 0):
            continue

        # now it's not empty!
        field_type = fields_info[field_name]['type']
        is_list_field = fields_info[field_name]['is_list']

        if isinstance(field_value, ast.AST):
            child = ASTNode(field_type, field_name)
            child.add_child(python_ast_to_parse_tree(field_value))
        elif type(field_value) is str or type(field_value) is int or \
                        type(field_value) is float or type(field_value) is object or \
                        type(field_value) is bool:
            # if field_type != type(field_value):
            #     print 'expect [%s] type, got [%s]' % (field_type, type(field_value))
            child = ASTNode(type(field_value), field_name, value=field_value)
        elif is_list_field:
            list_node_type = typename(field_type) + '*'
            child = ASTNode(list_node_type, field_name)
            for n in field_value:
                if field_type in {ast.comprehension, ast.excepthandler, ast.arguments, ast.keyword, ast.alias}:
                    child.add_child(python_ast_to_parse_tree(n))
                else:
                    intermediate_node = ASTNode(field_type)
                    if field_type is str:
                        intermediate_node.value = n
                    else:
                        intermediate_node.add_child(python_ast_to_parse_tree(n))
                    child.add_child(intermediate_node)

        else:
            raise RuntimeError('unknown AST node field!')

        tree.add_child(child)

    return tree


def parse_tree_to_python_ast(tree):
    node_type = tree.type
    node_label = tree.label

    # remove root
    if node_type == 'root':
        return parse_tree_to_python_ast(tree.children[0])

    ast_node = node_type()
    node_type_name = typename(node_type)

    # if it's a compositional AST node, populate its children nodes,
    # fill fields with empty(default) values otherwise
    if node_type_name in PY_AST_NODE_FIELDS:
        fields_info = PY_AST_NODE_FIELDS[node_type_name]

        for child_node in tree.children:
            # if it's a compositional leaf
            if child_node.type == 'epsilon':
                continue

            field_type = child_node.type
            field_label = child_node.label
            field_entry = fields_info[field_label]
            is_list = field_entry['is_list']

            if is_list:
                field_type = field_entry['type']
                field_value = []

                if field_type in {ast.comprehension, ast.excepthandler, ast.arguments, ast.keyword, ast.alias}:
                    nodes_in_list = child_node.children
                    for sub_node in nodes_in_list:
                        sub_node_ast = parse_tree_to_python_ast(sub_node)
                        field_value.append(sub_node_ast)
                else:  # expr stuffs
                    inter_nodes = child_node.children
                    for inter_node in inter_nodes:
                        if inter_node.value is None:
                            assert len(inter_node.children) == 1
                            sub_node_ast = parse_tree_to_python_ast(inter_node.children[0])
                            field_value.append(sub_node_ast)
                        else:
                            assert len(inter_node.children) == 0
                            field_value.append(inter_node.value)
            else:
                # this node either holds a value, or is an non-terminal
                if child_node.value is None:
                    assert len(child_node.children) == 1
                    field_value = parse_tree_to_python_ast(child_node.children[0])
                else:
                    assert child_node.is_leaf
                    field_value = child_node.value

            setattr(ast_node, field_label, field_value)

    for field in ast_node._fields:
        if not hasattr(ast_node, field) and not field in NODE_FIELD_BLACK_LIST:
            if fields_info and fields_info[field]['is_list'] and not fields_info[field]['is_optional']:
                setattr(ast_node, field, list())
            else:
                setattr(ast_node, field, None)

    return ast_node


def decode_tree_to_python_ast(decode_tree):
    decode_tree = decode_tree.children[0]
    terminals = decode_tree.get_leaves()

    for terminal in terminals:
        if terminal.value is not None and type(terminal.value) is str:
            if terminal.value.endswith('<eos>'):
                terminal.value = terminal.value[:-5]

        if terminal.type in {int, float, str, bool}:
            # cast to target data type
            terminal.value = terminal.type(terminal.value)

    ast_tree = parse_tree_to_python_ast(decode_tree)

    return ast_tree


p_elif = re.compile(r'^elif\s?')
p_else = re.compile(r'^else\s?')
p_try = re.compile(r'^try\s?')
p_except = re.compile(r'^except\s?')
p_finally = re.compile(r'^finally\s?')
p_decorator = re.compile(r'^@.*')


def canonicalize_code(code):
    if p_elif.match(code):
        code = 'if True: pass\n' + code

    if p_else.match(code):
        code = 'if True: pass\n' + code

    if p_try.match(code):
        code = code + 'pass\nexcept: pass'
    elif p_except.match(code):
        code = 'try: pass\n' + code
    elif p_finally.match(code):
        code = 'try: pass\n' + code

    if p_decorator.match(code):
        code = code + '\ndef dummy(): pass'

    if code[-1] == ':':
        code = code + 'pass'

    return code


def add_root(tree):
    root_node = ASTNode('root')
    root_node.add_child(tree)

    return root_node


def parse(code):
    """
    parse a python code into a tree structure
    code -> AST tree -> AST tree to internal tree structure
    """

    code = canonicalize_code(code)
    py_ast = ast.parse(code)

    tree = python_ast_to_parse_tree(py_ast.body[0])

    tree = add_root(tree)

    return tree

def parse_raw(code):
    py_ast = ast.parse(code)

    tree = python_ast_to_parse_tree(py_ast.body[0])

    tree = add_root(tree)

    return tree


def extract_grammar(code_file):
    line_num = 0
    parse_trees = []
    for line in open(code_file):
        code = line.strip()
        parse_tree = parse(code)

        # leaves = parse_tree.get_leaves()
        # for leaf in leaves:
        #     if not is_terminal_type(leaf.type):
        #         print parse_tree

        # parse_tree = add_root(parse_tree)

        parse_trees.append(parse_tree)

        # sanity check
        ast_tree = parse_tree_to_python_ast(parse_tree)
        ref_ast_tree = ast.parse(canonicalize_code(code)).body[0]
        source1 = astor.to_source(ast_tree)
        source2 = astor.to_source(ref_ast_tree)

        assert source1 == source2

        # check rules
        # rule_list = parse_tree.get_rule_list(include_leaf=True)
        # for rule in rule_list:
        #     if rule.parent.type == int and rule.children[0].type == int:
        #         # rule.parent.type == str and rule.children[0].type == str:
        #         pass

        # ast_tree = tree_to_ast(parse_tree)
        # print astor.to_source(ast_tree)
            # print parse_tree
        # except Exception as e:
        #     error_num += 1
        #     #pass
        #     #print e

        line_num += 1

    print 'total line of code: %d' % line_num

    grammar = get_grammar(parse_trees)

    with open('grammar.txt', 'w') as f:
        for rule in grammar:
            str = rule.__repr__()
            f.write(str + '\n')

    with open('parse_trees.txt', 'w') as f:
        for tree in parse_trees:
            f.write(tree.__repr__() + '\n')

    return grammar, parse_trees


def get_grammar(parse_trees):
    rules = set()
    # rule_num_dist = defaultdict(int)

    for parse_tree in parse_trees:
        parse_tree_rules, rule_parents = parse_tree.get_productions()
        for rule in parse_tree_rules:
            rules.add(rule)

    rules = list(sorted(rules, key=lambda x: x.__repr__()))
    grammar = PythonGrammar(rules)

    logging.info('num. rules: %d', len(rules))

    return grammar


def tokenize_code(code):
    token_stream = generate_tokens(StringIO(code).readline)
    tokens = []
    for toknum, tokval, (srow, scol), (erow, ecol), _ in token_stream:
        if toknum == tk.ENDMARKER:
            break
        tokens.append(tokval)

    return tokens



if __name__ == '__main__':
    from nn.utils.generic_utils import init_logging
    init_logging('misc.log')

    # django_code_file = '/Users/yinpengcheng/Research/SemanticParsing/CodeGeneration/en-django/all.code'
    #
    # grammar, parse_trees = extract_grammar(django_code_file)
    # id = 1888
    # parse_tree = parse_trees[id]
    # print parse_tree
    # from components import Hyp
    # hyp = Hyp(grammar)
    # rules, rule_parents = parse_tree.get_productions()
    #
    # while hyp.frontier_nt():
    #     nt = hyp.frontier_nt()
    #     if grammar.is_value_node(nt):
    #         hyp.append_token('111<eos>')
    #     else:
    #         rule = rules[0]
    #         hyp.apply_rule(rule)
    #         del rules[0]
    #
    # print hyp
    #
    # ast_tree = decode_tree_to_python_ast(hyp.tree)
    # source = astor.to_source(ast_tree)
    # print source

    # for code in open(django_code_file):
    #     code = code.strip()
    #     ref_ast_tree = ast.parse(canonicalize_code(code)).body[0]
    #     parse_tree = parse(code)
    #     ast_tree = parse_tree_to_python_ast(parse_tree)
    #     source1 = astor.to_source(ast_tree)
    #     source2 = astor.to_source(ref_ast_tree)
    #
    #     if source1 != source2:
    #         pass

    code = """__all__ = [  '_STR:0_' , '_STR:1_' , '_STR:2_' , '_STR:3_' ,  'CacheKeyWarning' , 'BaseCache' ,  ]"""
    # code = """a = [1,2,3,4,'asdf', 234.3]"""
    parse_tree = parse(code)
    for leaf in parse_tree.get_leaves():
        if leaf.value: print escape(leaf.value)

    print parse_tree
    # ast_tree = parse_tree_to_python_ast(parse_tree)
    # print astor.to_source(ast_tree)