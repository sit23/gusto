from firedrake import (inner, outer, grad, avg, dx, dS_h, dS_v,
                       FacetNormal)
from gusto.form_manipulation_labelling import subject, diffusion


__all__ = ["interior_penalty_diffusion_form"]


def interior_penalty_diffusion_form(state, test, q, kappa, mu):
    """
    Interior penalty diffusion form

    :arg state: :class:`.State` object.
    :arg V: Function space of diffused field
    :arg direction: list containing directions in which function space
    :arg: mu: the penalty weighting function, which is
    :recommended to be proportional to 1/dx
    :arg: kappa: strength of diffusion

    """
    n = FacetNormal(state.mesh)

    form = inner(grad(test), grad(q)*kappa)*dx

    def get_flux_form(dS, M):

        fluxes = (
            -inner(2*avg(outer(q, n)), avg(grad(test)*M))
            - inner(avg(grad(q)*M), 2*avg(outer(test, n)))
            + mu*inner(2*avg(outer(q, n)), 2*avg(outer(test, n)*kappa))
        )*dS
        return fluxes

    form += get_flux_form(dS_v, kappa)
    form += get_flux_form(dS_h, kappa)

    form = subject(form, q)

    return diffusion(form)
