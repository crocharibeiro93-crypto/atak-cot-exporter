
import re
from html import escape
from qgis.PyQt.QtCore import QXmlStreamReader

MAX_BYTES = 32 * 1024 * 1024
MAX_NODES = 500000
MAX_DEPTH = 128

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*\Z")


def _qstr(value):
    if hasattr(value, "toString"):
        return str(value.toString())
    return str(value)


def _validate_text(value):
    value = str(value)

    for character in value:
        code = ord(character)
        if code < 32 and character not in "\t\n\r":
            raise ValueError("Carácter XML inválido.")

    return value


class Element:
    def __init__(self, tag, attrib=None):
        self.tag = str(tag)
        self.attrib = dict(attrib or {})
        self.text = None
        self.tail = None
        self.children = []

    def __iter__(self):
        return iter(self.children)

    def append(self, child):
        self.children.append(child)

    def remove(self, child):
        self.children.remove(child)

    def get(self, name, default=None):
        return self.attrib.get(name, default)


def SubElement(parent, tag, attrib=None):
    node = Element(tag, attrib)
    parent.append(node)
    return node


def fromstring(data):
    if isinstance(data, str):
        data = data.encode("utf-8")

    if len(data) > MAX_BYTES:
        raise ValueError("Manifest XML demasiado grande.")

    reader = QXmlStreamReader(data)

    root = None
    stack = []
    nodes = 0

    while not reader.atEnd():
        token = reader.readNext()

        # Rejeitar DTD e entidades declaradas
        if token in (
            QXmlStreamReader.TokenType.DTD,
            QXmlStreamReader.TokenType.EntityReference
        ):
            raise ValueError(
                "DTD ou entidade XML não permitida."
            )

        if reader.isStartElement():
            if _qstr(reader.namespaceUri()):
                raise ValueError(
                    "Namespaces XML não suportados."
                )

            nodes += 1

            if nodes > MAX_NODES:
                raise ValueError(
                    "Número excessivo de elementos XML."
                )

            if len(stack) >= MAX_DEPTH:
                raise ValueError(
                    "Profundidade XML excessiva."
                )

            attrs = {
                _qstr(a.name()): _qstr(a.value())
                for a in reader.attributes()
            }

            node = Element(
                _qstr(reader.name()), attrs
            )

            if stack:
                stack[-1].append(node)
            else:
                if root is not None:
                    raise ValueError(
                        "XML com múltiplas raízes."
                    )
                root = node

            stack.append(node)

        elif reader.isCharacters():
            if stack:
                value = _qstr(reader.text())

                if stack[-1].children:
                    previous = stack[-1].children[-1]
                    previous.tail = (
                        (previous.tail or "") + value
                    )
                else:
                    current = stack[-1]
                    current.text = (
                        (current.text or "") + value
                    )

        elif reader.isEndElement():
            if not stack:
                raise ValueError(
                    "Fecho XML inesperado."
                )
            stack.pop()

    if reader.hasError():
        raise ValueError(
            "Manifest XML inválido: "
            + reader.errorString()
        )

    if root is None or stack:
        raise ValueError(
            "Manifest XML incompleto."
        )

    return root


def _serialize(node):
    if not NAME.fullmatch(node.tag):
        raise ValueError(
            "Nome de elemento XML inválido."
        )

    attrs = []

    for key, value in node.attrib.items():
        key = str(key)

        if not NAME.fullmatch(key):
            raise ValueError(
                "Nome de atributo XML inválido."
            )

        attrs.append(
            ' {}="{}"'.format(
                key,
                escape(
                    _validate_text(value), quote=True
                )
            )
        )

    opening = "<{}{}>".format(
        node.tag, "".join(attrs)
    )

    content = ""

    if node.text is not None:
        content += escape(
            _validate_text(node.text),
            quote=False
        )

    for child in node:
        content += _serialize(child)

    result = opening + content + "</{}>".format(
        node.tag
    )

    if node.tail is not None:
        result += escape(
            _validate_text(node.tail),
            quote=False
        )

    return result


def tostring(node, encoding="utf-8"):
    if encoding.lower() != "utf-8":
        raise ValueError(
            "Apenas UTF-8 é suportado."
        )

    return _serialize(node).encode("utf-8")
