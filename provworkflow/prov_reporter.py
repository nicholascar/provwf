import logging
import os

# import signal
import uuid
from _datetime import datetime
from typing import Union

import requests

# from franz.openrdf.connect import ag_connect
# from franz.openrdf.rio.rdfformat import RDFFormat
from rdflib import Graph, URIRef, Literal
from rdflib.namespace import DCTERMS, PROV, OWL, RDF, RDFS, XSD

from .exceptions import ProvWorkflowException
from .namespace import PROVWF, PWFS
from .utils import make_sparql_insert_data, query_sop_sparql, get_version_uri


class class_or_instance_method(classmethod):
    def __get__(self, instance, type_):
        descr_get = super().__get__ if instance is None else self.__func__.__get__
        return descr_get(instance, type_)


class ProvReporter:
    """Created provwf:ProvReporter instances.

    For its Semantic Web definition, see https://data.surroundaustralia.com/def/provworkflow/ProvReporter
     (not available yet)

    ProvReporter is a superclass of all PROV classes (Entity, Activity, Agent) and is created here just to facilitate
    logging. You should NOT directly instantiate this class - it is essentially abstract. Use instead, Entity, Activity
    etc., including grandchildren such as Block & Workflow.

    ProvReporters automatically record created times (dcterms:created) and an instance version IRI which is collected
    from the instance's Git version (URI of the Git origin repo, not local).

    :param uri: A URI you assign to the ProvReporter instance. If None, a UUID-based URI will be created,
    defaults to None
    :type uri: Union[URIRef, str], optional

    :param label: A text label you assign, defaults to None
    :type label: str, optional

    :param named_graph_uri: A Named Graph URI you assign, defaults to None
    :type named_graph_uri: Union[URIRef, str], optional
    """

    def __init__(
        self,
        uri: Union[URIRef, str] = None,
        label: Union[Literal, str] = None,
        named_graph_uri: Union[URIRef, str] = None,
        class_uri: Union[URIRef, str] = None,
    ):
        # give it an opaque UUID-based URI if one not given
        if uri is not None:
            self.uri = URIRef(uri) if type(uri) == str else uri
        else:
            self.uri = URIRef(PWFS + str(uuid.uuid1()))
        self.label = Literal(label) if type(label) == str else label
        self.named_graph_uri = (
            URIRef(named_graph_uri) if type(named_graph_uri) == str else named_graph_uri
        )

        # class specialisations
        if class_uri is not None:
            self.class_uri = URIRef(class_uri) if type(class_uri) == str else class_uri

            known_classes = ["Entity", "Activity", "Agent", "Workflow", "Block"]
            if self.__class__.__name__ in known_classes and self.class_uri is not None:
                raise ProvWorkflowException(
                    "If a ProvWorkflow-defined class is used without specialisation, class_uri must not be set"
                )
            elif (
                self.__class__.__name__ not in known_classes and self.class_uri is None
            ):
                raise ProvWorkflowException(
                    "A specialised Block must have a class_uri instance variable supplied"
                )
            elif self.class_uri is not None and not self.class_uri.startswith("http"):
                raise ProvWorkflowException(
                    "If supplied, a class_uri must start with http"
                )

        # from Git info
        uri_str = get_version_uri()
        if uri_str is not None:
            self.version_uri = URIRef(uri_str)
        self.created = Literal(
            datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
            datatype=XSD.dateTimeStamp,
        )

    def prov_to_graph(self, g: Graph = None) -> Graph:
        if g is None:
            if self.named_graph_uri is not None:
                g = Graph(identifier=URIRef(self.named_graph_uri))
            else:
                g = Graph()
        g.bind("prov", PROV)
        g.bind("provwf", PROVWF)
        g.bind("pwfs", PWFS)
        g.bind("owl", OWL)
        g.bind("dcterms", DCTERMS)

        # this instance's URI
        g.add((self.uri, RDF.type, PROVWF.ProvReporter))
        g.add((self.uri, DCTERMS.created, self.created))

        # add a label if this Activity has one
        if self.label is not None:
            g.add((self.uri, RDFS.label, Literal(self.label, datatype=XSD.string)))

        return g

    @classmethod
    def _persist_to_file(
        cls,
        g: Graph,
        rdf_file_path: str = "prov_reporter",
        named_graph_uri: Union[URIRef, str] = None,
    ):
        # remove file extension if added as system will add appropriate one
        rdf_file_path = rdf_file_path.replace(".ttl", "")
        rdf_file_path = rdf_file_path.replace(".trig", "")

        if named_graph_uri is None:
            g.serialize(destination=rdf_file_path + ".ttl", format="turtle")
        else:
            g.serialize(destination=rdf_file_path + ".trig", format="trig")

    @classmethod
    def _persist_to_graphdb(cls, g: Graph, named_graph_uri: Union[URIRef, str] = None):
        """
        generic util to write a given graph to graphdb
        :param context: the named graph to add the triples to
        :param graph:
        :return: a status code
        """
        GRAPH_DB_SYSTEM_URI = os.environ.get(
            "GRAPH_DB_SYSTEM_URI", "http://localhost:7200"
        )
        GRAPH_DB_REPO_ID = os.environ.get("GRAPH_DB_REPO_ID", "provwftesting")
        GRAPHDB_USR = os.environ.get("GRAPHDB_USR", "")
        GRAPHDB_PWD = os.environ.get("GRAPHDB_PWD", "")

        data = g.serialize(format="turtle", encoding="utf-8")

        # graphdb expects the context (named graph) wrapped in < & >
        if named_graph_uri != "null":
            if named_graph_uri is None:
                context = "null"
            else:
                context = "<" + str(named_graph_uri) + ">"

        r = requests.post(
            GRAPH_DB_SYSTEM_URI + "/repositories/" + GRAPH_DB_REPO_ID + "/statements",
            params={"context": context},
            data=data,
            headers={"Content-Type": "text/turtle"},
            auth=(GRAPHDB_USR, GRAPHDB_PWD),
        )
        logging.info(
            f"Attempted to write triples to GraphDB and got status code: {r.status_code} returned"
        )
        if r.status_code != 204:
            raise Exception(f"GraphDB says: {r.text}")

    @classmethod
    def _persist_to_sop(cls, g: Graph, named_graph_uri: Union[URIRef, str] = None):
        """
        generic util to write a given graph to graphdb
        :param named_graph_uri: the data graph to add the triples to
        :param graph: the graph to be written out
        :return: a status code
        """
        # TODO: determine if we need to set a default SOP graph ID
        # if self.named_graph_uri is None:
        #     self.named_graph_uri = "http://example.com"
        query = make_sparql_insert_data(named_graph_uri, g)
        r = query_sop_sparql(named_graph_uri, query, update=True)

        logging.info(
            f"Attempted to write triples to SOP and got status code: {r.status_code} returned"
        )
        if not r.ok:
            raise Exception(f"SOP HTTP error: {r.text}")

    # TODO: retest this method as needed
    # @classmethod
    # def _persist_to_allegro(self, g: Graph):
    #     """Sends the provenance graph of this Workflow to an AllegroGraph instance as a Turtle string
    #
    #     The URI assigned to the Workflow us used for AllegroGraph context (graph URI) or a Blank Node is generated, if
    #     one is not given.
    #
    #     The function will error out if connection & transfer not complete after 5 seconds.
    #
    #     Environment variables are required for connection details.
    #
    #     :return: None
    #     :rtype: None
    #     """
    #     if g is None:
    #         g = self.prov_to_graph()
    #
    #     vars = [
    #         os.environ.get("ALLEGRO_REPO"),
    #         os.environ.get("ALLEGRO_HOST"),
    #         os.environ.get("ALLEGRO_PORT"),
    #         os.environ.get("ALLEGRO_USER"),
    #         os.environ.get("ALLEGRO_PASSWORD"),
    #     ]
    #     assert all(v is not None for v in vars), (
    #         "You must set the following environment variables: "
    #         "ALLEGRO_REPO, ALLEGRO_HOST, ALLEGRO_PORT, ALLEGRO_USER & "
    #         "ALLEGRO_PASSWORD"
    #     )
    #
    #     def connect_and_send():
    #         with ag_connect(
    #             os.environ["ALLEGRO_REPO"],
    #             host=os.environ["ALLEGRO_HOST"],
    #             port=int(os.environ["ALLEGRO_PORT"]),
    #             user=os.environ["ALLEGRO_USER"],
    #             password=os.environ["ALLEGRO_PASSWORD"],
    #         ) as conn:
    #             conn.addData(
    #                 g.serialize(format="turtle").decode("utf-8"),
    #                 rdf_format=RDFFormat.TURTLE,
    #                 context=conn.createURI(self.uri) if self.uri is not None else None,
    #             )
    #
    #     def handler(signum, frame):
    #         raise Exception("Connecting to AllegroGraph failed")
    #
    #     signal.signal(signal.SIGALRM, handler)
    #
    #     signal.alarm(5)
    #
    #     try:
    #         connect_and_send()
    #     except Exception as exc:
    #         print(exc)

    # # see http://192.168.0.132:10035/doc/python/tutorial/example006.html
    # def send_file_to_allegro(self, turtle_file_path, context_uri=None):
    #     """Sends an RDF file, with or without a given Context URI to AllegroGraph.
    #
    #     The function will error out if connection & transfer not complete after 5 seconds.
    #
    #     Environment variables are required for connection details."""
    #
    #     vars = [
    #         os.environ.get('ALLEGRO_REPO'),
    #         os.environ.get('ALLEGRO_HOST'),
    #         os.environ.get('ALLEGRO_PORT'),
    #         os.environ.get('ALLEGRO_USER'),
    #         os.environ.get('ALLEGRO_PASSWORD')
    #     ]
    #     assert all(v is not None for v in vars), "You must set the following environment variables: " \
    #                                              "ALLEGRO_REPO, ALLEGRO_HOST, ALLEGRO_PORT, ALLEGRO_USER & " \
    #                                              "ALLEGRO_PASSWORD"
    #
    #     def connect_and_send():
    #         with ag_connect(
    #                 os.environ['ALLEGRO_REPO'],
    #                 host=os.environ['ALLEGRO_HOST'],
    #                 port=int(os.environ['ALLEGRO_PORT']),
    #                 user=os.environ['ALLEGRO_USER'],
    #                 password=os.environ['ALLEGRO_PASSWORD'],
    #         ) as conn:
    #             conn.addFile(
    #                 turtle_file_path,
    #                 rdf_format=RDFFormat.TURTLE,
    #                 context=conn.createURI(context_uri) if context_uri is not None else None,
    #             )
    #
    #     def handler(signum, frame):
    #         raise Exception("Connecting to AllegroGraph failed")
    #
    #     signal.signal(signal.SIGALRM, handler)
    #
    #     signal.alarm(5)
    #
    #     try:
    #         connect_and_send()
    #     except Exception as exc:
    #         print(exc)

    @class_or_instance_method
    def persist(
        cls_or_self,
        g: Union[Graph] = None,
        methods: Union[str, list] = None,
        rdf_file_path: str = "prov_reporter",
        named_graph_uri: Union[URIRef, str] = None,
    ) -> Union[None, str]:
        """This class method persists a given RDFlib Graph according to one or more given methods."""
        if type(methods) == str:
            methods = [methods]
        elif methods is None:
            methods = ["string"]

        known_methods = ["graphdb", "sop", "file", "string"]  # "allegro",
        for method in methods:
            if method not in known_methods:
                raise ProvWorkflowException(
                    "A persistent method you selected, {}, is not in the list of known methods, '{}'".format(
                        method, "', '".join(known_methods)
                    )
                )

        # if called on an instance, get the graph from this instance's graph generation method
        if not isinstance(cls_or_self, type):
            g = cls_or_self.prov_to_graph()
            if cls_or_self.named_graph_uri is not None:
                named_graph_uri = cls_or_self.named_graph_uri
        else:
            if g is None:
                raise ProvWorkflowException(
                    "When called as a class method, i.e. ProvReporter.persist(...), you must supply a non-null Graph g"
                )

        # write to one or more persistence layers
        if "file" in methods:
            ProvReporter._persist_to_file(g, rdf_file_path, named_graph_uri)
        if "graphdb" in methods:
            ProvReporter._persist_to_graphdb(g, named_graph_uri)
        if "sop" in methods:
            ProvReporter._persist_to_sop(g, named_graph_uri)
        # if "allegro" in methods:
        #     ProvReporter._persist_to_allegro(g)

        # final persistent option
        if "string" in methods:
            if named_graph_uri is None:
                x = g.serialize(format="turtle")
            else:
                x = g.serialize(format="trig").decode()

            return x if type(x) is str else x.decode()
