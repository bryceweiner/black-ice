// A status line rather than a copyright notice: on an always-on wall display
// the useful thing to know at the bottom of the page is whether the feed is
// still alive.

import React from "react";
import { Col, Container, Row } from "reactstrap";
import { useConnected } from "../../../blackice/live";

const Footer = () => {
  const connected = useConnected();

  return (
    <footer className="footer">
      <Container fluid>
        <Row>
          <Col md="6" className="footer-copyright">
            <p className="mb-0 text-muted small">Black Ice · everything stays on this network</p>
          </Col>
          <Col md="6">
            <p className="pull-right mb-0 small d-flex align-items-center justify-content-end gap-2">
              <span
                className={`rounded-circle ${connected ? "bg-success" : "bg-danger"}`}
                style={{ width: 7, height: 7, display: "inline-block" }}
              />
              <span className="text-muted">{connected ? "Live" : "Reconnecting…"}</span>
            </p>
          </Col>
        </Row>
      </Container>
    </footer>
  );
};

export default Footer;
