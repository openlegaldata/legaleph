import logging
from collections import defaultdict
from pprint import pformat  # noqa

from banal import ensure_list
from followthemoney import model
from followthemoney.graph import Graph
from followthemoney.types import registry

from aleph.core import es
from aleph.model import Entity
from aleph.logic import resolver
from aleph.index.util import unpack_result
from aleph.index.util import authz_query, field_filter_query
from aleph.index.indexes import entities_read_index
from aleph.index import entities as index


log = logging.getLogger(__name__)


class AlephGraph(Graph):

    def resolve(self):
        for id_, proxy in self.proxies.items():
            if proxy is None:
                resolver.queue(self, Entity, id_)
        resolver.resolve(self)
        for id_, proxy in list(self.proxies.items()):
            if proxy is not None:
                continue
            entity = resolver.get(self, Entity, id_)
            self.add(model.get_proxy(entity))

    def get_adjacent_entities(self, proxy):
        source_node_id = registry.entity.node_id_safe(proxy.id)
        adjacents = defaultdict(list)
        expanded_prop_nodes = []
        for edge in self.edges.values():
            if edge.source_id == source_node_id:
                if edge.target.is_entity:
                    adjacents[edge.type_name].append(edge.target.proxy)
                else:
                    expanded_prop_nodes.append(edge.target_id)
            if edge.target_id == source_node_id:
                if edge.source.is_entity:
                    adjacents[edge.type_name].append(edge.source.proxy)
        for edge in self.edges.values():
            if (edge.target_id in expanded_prop_nodes
                    and edge.source_id != source_node_id):
                adjacents[edge.type_name].append(edge.source.proxy)
        for prop, proxies in adjacents.items():
            yield (prop, len(proxies), proxies)


class EntityGraphResponse(object):
    """ES graph query results as property wise adjacent entities and
    counts"""

    def __init__(self, proxy):
        self.entities = dict()
        self.counts = dict()
        # The entity being queried for tags, references or expansion
        self.source = proxy

    def set_entities(self, qname, entities):
        self.entities[qname] = ensure_list(entities)

    def set_count(self, qname, count):
        self.counts[qname] = count

    def get_count(self, qname):
        return self.counts.get(qname) or 0

    @property
    def total_count(self):
        return sum(self.count(qname) for qname in self.entities)

    def iter_prop_counts(self):
        for qname, count in self.counts.items():
            if count > 0:
                yield (model.get_qname(qname), count)

    def iter_props(self):
        for qname, count in self.counts.items():
            if count > 0:
                prop = model.get_qname(qname)
                # Match the  type_name in FtM Edge class
                edge_name = prop.name if not prop.stub else prop.range.name
                yield (edge_name, count, self.entities.get(qname, []))

    def reverse_properties(self, qnames=None):
        """Use the reverse property names.

        While querying for references, property names in the query result
        are in the form of `edgeSchema:property` (eg: Directorship:director).
        But while preparing response for entity expansion, we want to return
        the property from the reverse direction instead (eg:
        LegalEntity:directorshipDirector)

        This method replaces property keys with their reverse properties.
        """
        qnames = qnames or list(self.counts.keys())
        for qname in set(qnames):
            prop = model.get_qname(qname)
            reversed_prop = prop.reverse
            entities = self.entities.pop(qname, None)
            if entities is not None:
                self.set_entities(reversed_prop.qname, entities)
            count = self.counts.pop(qname, None)
            if count is not None:
                self.set_count(reversed_prop.qname, count)

    def ignore_source(self, qnames=None):
        """Ignore counting the source entity itself (useful when querying
        for tags.)

        For example, when querying for all Companies with the same `email` as
        `CompanyA`, `CompanyA` will also appear in the results. We want to
        reduce all counts by 1 and remove the expansion source entity
        (CompanyA in this case) from the query result.
        """
        qnames = qnames or list(self.counts.keys())
        for qname in set(qnames):
            count = self.counts.get(qname, None)
            if count:
                # Reduce count by 1
                count = count - 1
                if count == 0:
                    self.counts.pop(qname, None)
                else:
                    self.set_count(qname, count)
            # Remove source entity from matched entities
            entities = self.entities.pop(qname, None)
            if entities:
                entities = [ent for ent in entities if ent['id'] != self.source.id]  # noqa
                if entities:
                    self.set_entities(qname, entities)
                else:
                    self.entities.pop(qname, None)

    def merge(self, resp):
        if self.source != resp.source:
            raise ValueError("Responses don't have the same source entity")
        self.entities.update(resp.entities)
        self.counts.update(resp.counts)
        return self


class EntityGraph(object):

    def __init__(self, proxy, edge_types=None, included_properties=None,
                 collection_ids=None, authz=None, limit=None):
        self.proxy = proxy
        self.collection_ids = collection_ids
        self.authz = authz
        # Properties to consider when expanding the entity
        self.included_properties = included_properties or []
        # Property Types to consider when expanding the entity
        edge_types = edge_types or []
        self.edge_types = [t for t in registry.get_types(edge_types) if t.matchable]  # noqa
        self.limit = limit

    def expand_entity(self):
        """Expand adjacent entities of the source entity

        An entity can have 3 types of adjacent entities:
        1. Edge entities that refer to the source entity (references) - eg:
           Directorships, Ownerships etc
        2. Entities that share a matchable property with the source entity
           (tags) - eg: Entities with same phone number, email, name etc
        3. Entities directly attached to the source entity as a property value
           (direct links) - eg: Passport attached to a Person
        """
        # Get references
        reference_facets = self._get_reference_facets()
        reference_props = [facet[1] for facet in reference_facets]
        # get tags
        # Group tagged results by property name - (eg: 2 email matches,
        # 1 website match etc)
        tags_facets = self._get_tag_facets_grouped_by_prop_name()
        tags_props = [facet[1] for facet in tags_facets]
        facets = reference_facets + tags_facets
        expanded_entities = self._get_graph_response(facets)
        # Remove the entity we are exapnding from tag results
        expanded_entities.ignore_source(qnames=tags_props)
        # Reverse the property direction for all reference properties
        # eg: from Ownership:owner to LegalEntity:ownershipOwner
        expanded_entities.reverse_properties(qnames=reference_props)
        # get direct links
        direct_links = self.get_direct_links()
        # adjacent entities = tags + references + direct links
        expanded_entities = expanded_entities.merge(direct_links)
        return expanded_entities.iter_props()

    def get_references(self):
        facets = self._get_reference_facets()
        graph_resp = self._get_graph_response(facets)
        return graph_resp.iter_prop_counts()

    def _get_reference_facets(self):
        facets = []
        schema = self.proxy.schema
        for prop in model.properties:
            # TODO: do we want a model.get_prop_by_range(schema) method?
            if self.included_properties and prop.qname not in self.included_properties:  # noqa
                continue
            if prop.type != registry.entity:
                continue
            if not schema.is_a(prop.range):
                continue
            index = entities_read_index(prop.schema)
            field = 'properties.%s' % prop.name
            value = self.proxy.id
            facets.append(
                (index, prop.qname, registry.entity.group, field, value)
            )
        return facets

    def get_tags(self):
        # Tags are grouped by property value (not by type) - eg:
        # 2 matches for email a@example.com, 3 matches for email b@ex.com etc
        facets = self._get_tag_facets_grouped_by_prop_value()
        graph_resp = self._get_graph_response(facets)
        graph_resp.ignore_source()
        for (_, alias, field, _, value) in facets:
            total = graph_resp.get_count(alias)
            if total > 0:
                yield (field, value, total)

    def _get_tag_facets_grouped_by_prop_value(self):
        facets = []
        Thing = model.get(Entity.THING)
        # Go through all the tags which apply to this entity, and find how
        # often they've been mentioned in other entities.
        for type_ in self.edge_types:
            if type_.group is None:
                continue
            for fidx, value in enumerate(self.proxy.get_type_values(type_)):
                if type_.specificity(value) < 0.1:
                    continue
                schemata = model.get_type_schemata(type_)
                schemata = [s for s in schemata if s.is_a(Thing)]
                index = entities_read_index(schemata)
                alias = '%s_%s' % (type_.name, fidx)
                facets.append((index, alias, type_.group, type_.group, value))
        return facets

    def _get_tag_facets_grouped_by_prop_name(self):
        facets = []
        for prop in model.properties:
            if prop.type not in self.edge_types:
                continue
            # Check if we're expanding all properties or a limited
            # list of properties only
            if self.included_properties and prop.qname not in self.included_properties:  # noqa
                continue
            if prop.type == registry.entity or prop.stub is True:
                continue
            if not self.proxy.schema.is_a(prop.schema):
                continue
            values = self.proxy.get(prop.name)
            index = entities_read_index(prop.schema)
            field = 'properties.%s' % prop.name
            for val in values:
                facets.append((index, prop.qname, prop.type.group, field, val))
        return facets

    def get_direct_links(self):
        graph_resp = EntityGraphResponse(self.proxy)
        for prop in model.properties:
            if prop.type != registry.entity or prop.stub:
                continue
            if not self.proxy.schema.is_a(prop.schema):
                continue
            values = self.proxy.get(prop.name)
            total = len(values)
            if total > 0:
                if self.limit > 0:
                    entities = [index.get_entity(val) for val in values]
                    graph_resp.set_entities(prop.qname, entities)
                graph_resp.set_count(prop.qname, total)
        return graph_resp

    def _build_es_queries(self, facets):
        filters = {}
        indexed = {}
        for (idx, alias, group, field, value) in facets:
            indexed[idx] = indexed.get(idx, {})
            indexed[idx][alias] = indexed[idx].get(alias, {})
            indexed[idx][alias]['field'] = field
            indexed[idx][alias]['values'] = indexed[idx][alias].get('values', [])  # noqa
            indexed[idx][alias]['values'].append(value)
            filters[idx] = filters.get(idx, {})
            filters[idx][group] = filters[idx].get(group, [])
            filters[idx][group].append(value)

        queries = []
        for (idx, facets) in indexed.items():
            query = []
            if self.authz is not None:
                query.append(authz_query(self.authz))
            if self.collection_ids:
                query.append(field_filter_query('collection_id', self.collection_ids))  # noqa
            shoulds = []
            for field, values in filters[idx].items():
                shoulds.append(field_filter_query(field, values))
            query = {
                'bool': {
                    'should': shoulds,
                    'filter': query,
                    'minimum_should_match': 1
                }
            }
            for (alias, data) in facets.items():
                field = data['field']
                values = data['values']
                queries.append({'index': idx})
                aggs = {'counters': {
                    'filters': {'filters': {
                        alias: field_filter_query(field, values)
                    }}
                }}
                queries.append({
                    'size': self.limit or 0,
                    'query': query,
                    'aggs': aggs
                })
        return queries

    def _get_graph_response(self, facets):
        queries = self._build_es_queries(facets)
        res = es.msearch(body=queries)
        graph_response = EntityGraphResponse(self.proxy)
        for resp in res.get('responses', []):
            aggs = resp.get('aggregations', {}).get('counters', {})
            for alias, value in aggs.get('buckets', {}).items():
                count = value.get('doc_count', graph_response.get_count(alias))
                if count > 0:
                    graph_response.set_count(alias, count)
                    entities = []
                    hits = resp.get('hits', {}).get('hits', [])
                    for doc in hits:
                        entity = unpack_result(doc)
                        if entity is not None:
                            entities.append(entity)
                    graph_response.set_entities(alias, entities)
        return graph_response