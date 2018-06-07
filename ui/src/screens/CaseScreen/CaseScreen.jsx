import React, { Component } from 'react';
import { connect } from "react-redux";

import { Screen, Breadcrumbs } from 'src/components/common';
import CaseContext from "src/components/Case/CaseContext";
import { Toolbar, CollectionSearch } from 'src/components/Toolbar';
import { fetchCollection } from "src/actions";
import { selectCollection } from "src/selectors";

class CaseScreen extends Component {
  componentDidMount() {
    const { collectionId } = this.props;
    this.props.fetchCollection({ id: collectionId });
  }

  componentDidUpdate(prevProps) {
    const { collectionId } = this.props;
    if (collectionId !== prevProps.collectionId) {
      this.props.fetchCollection({ id: collectionId });
    }
  }

  render() {
    const {collection} = this.props;
    return (
      <Screen title={collection.label}
              breadcrumbs={<Breadcrumbs collection={collection}/>}
              className='CaseScreen'>
        <CaseContext collection={collection} activeTab='Home'>
          <Toolbar>
            <CollectionSearch collection={collection} />
          </Toolbar>
          { 'this is the case home page' }
        </CaseContext>
      </Screen>
    );
  }
}

const mapStateToProps = (state, ownProps) => {
  const { collectionId } = ownProps.match.params;
  return {
    collectionId,
    collection: selectCollection(state, collectionId) };
};

CaseScreen = connect(mapStateToProps, {fetchCollection})(CaseScreen);
export default CaseScreen;
