import React, { Fragment, useEffect } from "react";
import Loader from "./component/common/loader/loader";
import Header from "./component/common/header/header";
import Sidebar from "./component/common/sidebar/sidebar";
import Rightsidebar from "./component/common/sidebar/rightsidebar";
import Footer from "./component/common/footer/footer";
import ThemeCustomize from "./component/common/theme-customizer/themeCustomize";
import { ToastContainer } from "react-toastify";
import { Outlet, useLocation } from "react-router-dom";
import ConfigDB from "./data/customizer/config";
import { CSSTransition, TransitionGroup } from "react-transition-group";
import { LiveProvider } from "./blackice/live";
import Console from "./blackice/Console";

const App = ({ assistant = "Ice" }) => {
  const animation = localStorage.getItem("animation") || ConfigDB.data.router_animation || "fade";
  const location = useLocation();
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [location.pathname]);

  return (
    <LiveProvider>
      <Loader />
      <div className="page-wrapper">
        <div className="page-body-wrapper">
          <Header />
          <Sidebar />
          <Rightsidebar />
          <div className="page-body">
            <TransitionGroup>
              <CSSTransition timeout={100} classNames={animation} unmountOnExit>
                <div>
                  <Outlet />
                </div>
              </CSSTransition>
            </TransitionGroup>
          </div>
          <Footer />
          <ThemeCustomize />
        </div>
      </div>
      <ToastContainer />
      <Console assistant={assistant} />
    </LiveProvider>
  );
};

export default App;
