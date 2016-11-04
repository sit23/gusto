from __future__ import absolute_import
from abc import ABCMeta, abstractmethod
from firedrake import Function, TestFunction, TrialFunction, \
    FacetNormal, \
    dx, dot, grad, div, jump, avg, dS, dS_v, dS_h, inner, \
    outer, sign, cross, CellNormal, as_vector, sqrt, Constant, \
    curl, warning, BrokenElement, FunctionSpace, Projector


class Equation(object):
    """
    Base class for equations in Gusto.

    :arg state: :class:`.State` object.
    """
    __metaclass__ = ABCMeta

    def __init__(self, state, V, **kwargs):
        self.state = state
        self.V = V

        # get parameters from kwargs:
        self.continuity = kwargs.get("continuity")
        self.linear = "linear_ref" in kwargs
        if self.linear:
            self.qbar = kwargs.get("linear_ref")
        self.ibp_twice = kwargs.get("ibp_twice", False)
        supg = "supg" in kwargs
        emb_dg = "embedded_dg_space" in kwargs

        if emb_dg:
            Vdg_elt = BrokenElement(V.ufl_element())
            Vdg = FunctionSpace(state.mesh, Vdg_elt)
            space = Vdg
            self.xdg_out = Function(Vdg)
            self.x_projected = Function(V)
            pparameters = {'ksp_type':'cg',
                           'pc_type':'bjacobi',
                           'sub_pc_type':'ilu'}
            self.Projector = Projector(self.xdg_out, self.x_projected,
                                       solver_parameters=pparameters)

        else:
            space = V

        # set up functions required for forms
        self.ubar = Function(state.V[0])
        self.test = TestFunction(space)
        self.trial = TrialFunction(space)
        self.q = Function(space)

        # find out if we are DG
        element = space.fiat_element
        self.dg = element.entity_dofs() == element.entity_closure_dofs()

        # DG and embedded DG methods need surface measures
        if self.dg:
            if space.extruded:
                self.dS = (dS_h + dS_v)
            else:
                self.dS = dS
        else:
            self.dS = None

        # DG and SUPG methods need n and un
        if self.dg or supg:
            self.n = FacetNormal(state.mesh)
            self.un = 0.5*(dot(self.ubar, self.n) + abs(dot(self.ubar, self.n)))

        if supg:
            # if using SUPG we must integrate by parts twice
            if not self.ibp_twice:
                warning("if using SUPG we must integrate by parts twice")
                self.ibp_twice = True

            # set default SUPG parameters
            dt = state.timestepping.dt
            supg_params = kwargs.get("supg").copy() if kwargs.get("supg") else {}
            supg_params.setdefault('a0', dt/sqrt(15.))
            supg_params.setdefault('a1', dt/sqrt(15.))
            supg_params.setdefault('a2', dt/sqrt(15.))
            supg_params.setdefault('dg_directions', [])

            # find out if we need to do DG upwinding in any direction and set
            # self.dS accordingly
            dg_interior_surfaces_dict = {0:dS_v, 1:dS_h}
            dg_interior_surfaces = [dg_interior_surfaces_dict[k] for k in supg_params["dg_directions"]]
            if len(dg_interior_surfaces) == 0:
                self.dS = None
            elif len(dg_interior_surfaces) == 1:
                self.dS = dg_interior_surfaces[0]
            elif len(dg_interior_surfaces) == 2:
                self.dS = dg_interior_surfaces[0] + dg_interior_surfaces[1]

            # make SUPG test function
            if(state.mesh.topological_dimension() == 2):
                taus = [supg_params["a0"], supg_params["a1"]]
                for i in supg_params["dg_directions"]:
                    taus[i] = 0.0
                tau = Constant(((taus[0], 0.), (0., taus[1])))
            elif(state.mesh.topological_dimension() == 3):
                taus = [supg_params["a0"], supg_params["a1"], supg_params["a2"]]
                for i in supg_params["dg_directions"]:
                    taus[i] = 0.0
                tau = Constant(((taus[0], 0., 0.), (0.,taus[1], 0.), (0., 0., taus[2])))
            dtest = dot(dot(self.ubar, tau), grad(self.test))
            self.test += dtest

    def mass_term(self, q):
        return inner(self.test, q)*dx

    @abstractmethod
    def advection_term(self):
        pass


class AdvectionEquation(Equation):

    def advection_term(self, q):

        if self.linear:
            qbar = self.qbar

            if self.dg:
                L = (-dot(grad(self.test), self.ubar)*qbar*dx +
                     jump(self.ubar*self.test, self.n)*avg(qbar)*self.dS)
            else:
                L = self.test*dot(self.ubar,self.state.k)*dot(self.state.k,grad(qbar))*dx

        else:
            if self.continuity:
                if self.ibp_twice:
                    L = inner(self.test, div(outer(q, self.ubar)))*dx
                else:
                    L = -inner(grad(self.test), outer(q, self.ubar))*dx
            else:
                if self.ibp_twice:
                    L = inner(outer(self.test,self.ubar),grad(q))*dx
                else:
                    L = -inner(div(outer(self.test,self.ubar)),q)*dx

            if self.dS is not None:
                L += dot(jump(self.test), (self.un('+')*q('+')
                                           - self.un('-')*q('-')))*self.dS
                if self.ibp_twice:
                    L -= (self.test('+')*dot(self.ubar('+'), self.n('+'))*q('+')
                          + self.test('-')*dot(self.ubar('-'),
                                               self.n('-'))*q('-'))*self.dS

        return L


class MomentumEquation(AdvectionEquation):

    def __init__(self, state, V, **kwargs):
        super(MomentumEquation, self).__init__(state, V)

        self.vector_invariant_form = kwargs.get("vector_invariant", None)

        self.n = FacetNormal(state.mesh)
        self.Upwind = 0.5*(sign(dot(self.ubar, self.n))+1)
        if V.extruded:
            self.dS = dS_v + dS_h
            self.perp = lambda u: as_vector([-u[1], u[0]])
            self.perp_u_upwind = lambda q: self.Upwind('+')*self.perp(q('+')) + self.Upwind('-')*self.perp(q('-'))
        else:
            self.dS = dS
            outward_normals = CellNormal(state.mesh)
            self.perp = lambda u: cross(outward_normals, u)
            self.perp_u_upwind = lambda q: self.Upwind('+')*cross(outward_normals('+'),q('+')) + self.Upwind('-')*cross(outward_normals('-'),q('-'))
        self.gradperp = lambda u: self.perp(grad(u))

    def advection_term(self, q):

        if self.vector_invariant_form is not None:
            if self.state.mesh.topological_dimension() == 3:

                # <w,curl(u) cross ubar + grad( u.ubar)>
                # =<curl(u),ubar cross w> - <div(w), u.ubar>
                # =<u,curl(ubar cross w)> -
                #      <<u_upwind, [[n cross(ubar cross w)cross]]>>

                both = lambda u: 2*avg(u)

                L = (
                    inner(q, curl(cross(self.ubar, self.test)))*dx
                    - inner(both(self.Upwind*q),
                            both(cross(self.n, cross(self.ubar, self.test))))*self.dS
                )

            else:

                if self.ibp_twice:
                    L = (
                        (-inner(self.test, div(self.perp(q))*self.perp(self.ubar)))*dx
                        - inner(jump(inner(self.test, self.perp(self.ubar)), self.n),
                                self.perp_u_upwind(q))*self.dS
                        + jump(inner(self.test,
                                     self.perp(self.ubar))*self.perp(q), self.n)*self.dS
                    )
                else:
                    L = (
                        -inner(self.gradperp(inner(self.test, self.perp(self.ubar))), q)*dx
                        - inner(jump(inner(self.test, self.perp(self.ubar)), self.n),
                                self.perp_u_upwind(q))*self.dS
                    )

            if self.vector_invariant_form is "EulerPoincare":
                L -= div(self.test)*inner(q, self.ubar)*dx

        else:
            L = super(MomentumEquation, self).advection_term(self, q)
        return L
