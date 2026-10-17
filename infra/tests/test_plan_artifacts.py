"""El apply en otro runner necesita el ZIP exacto generado durante plan."""

from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[2]


def test_zip_lambda_se_transfiere_del_plan_al_apply():
    accion = yaml.safe_load(
        (RAIZ / ".github/actions/plan-terraform/action.yml").read_text(encoding="utf-8")
    )
    workflow = yaml.safe_load(
        (RAIZ / ".github/workflows/infra-desplegar.yml").read_text(encoding="utf-8")
    )
    pasos_plan = accion["runs"]["steps"]
    subida = next(
        paso for paso in pasos_plan if paso.get("with", {}).get("name") == "lambda-platform"
    )
    assert subida["uses"] == "actions/upload-artifact@v4"
    assert subida["if"] == ("inputs.raiz == 'platform' && steps.revisar.outputs.cambios == 'true'")
    assert subida["with"]["include-hidden-files"] is True
    assert subida["with"]["if-no-files-found"] == "error"
    assert subida["with"]["retention-days"] == 1
    assert pasos_plan.index(subida) > next(
        i for i, paso in enumerate(pasos_plan) if paso.get("id") == "revisar"
    )

    pasos_apply = workflow["jobs"]["plataforma"]["steps"]
    descarga = next(
        paso for paso in pasos_apply if paso.get("with", {}).get("name") == subida["with"]["name"]
    )
    assert descarga["uses"] == "actions/download-artifact@v4"
    archivo = Path(subida["with"]["path"])
    assert archivo == Path("infra/platform/.terraform/pre_sign_up.zip")
    assert Path(descarga["with"]["path"]) / archivo.name == archivo
    assert pasos_apply.index(descarga) < next(
        i for i, paso in enumerate(pasos_apply) if paso.get("run") == "make infra-plataforma"
    )
